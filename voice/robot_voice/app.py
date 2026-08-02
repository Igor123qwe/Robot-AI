"""Главный цикл: слушаем → распознаём → отвечаем → говорим."""

from __future__ import annotations

import collections
import contextlib
import difflib
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import intents
from .audio import Listener, make_source
from .brain import Brain
from .busyflag import BusyFlag
from .config import Config
from .notes import Notes
from .ros import Ros
from .state import State
from .stt import Recognizer
from .tools import CUTOFF_VOLT, Timers, build_tools
from .tts import SentenceBuffer, Speaker

log = logging.getLogger("robot_voice")

# Мусор, который whisper любит выдавать на тишине и шуме. Русские модели
# обучались в том числе на субтитрах, поэтому на пустой записи выдают титры
# переводчиков — их ловим по характерным словам, а не списком целиком.
_JUNK = re.compile(r"^[\s.,!?…\-—\"'()]*$")
_HALLUCINATION = re.compile(
    r"субтитр|продолжение следует|спасибо за просмотр|редактор субтитров|"
    r"корректор|перевод[аи]? выполн|dimatorzok|amara\.org",
    re.I,
)

# Слова, которыми зовут перед именем: «эй, робот», «слушай, робот».
_FILLERS = {"эй", "ей", "хэй", "слушай", "слушайте", "окей", "ok", "окэй", "привет"}

# Через сколько молчания забываем прошлый разговор. Иначе вечером «а повтори»
# продолжает утреннюю тему, да и весь этот контекст оплачивается заново.
FORGET_SECONDS = 600.0

# Сколько слов допустимо в реплике без имени, пока открыто окно разговора.
# Живое продолжение разговора короткое; длинная фраза из комнаты — это почти
# всегда телевизор или разговор не с роботом.
IN_SESSION_WORDS = 10

# Сколько молчать с приветствием после недавнего перезапуска.
GREET_SILENCE = 1800.0

# Сколько времени «отмена» отменяет последнее действие, а не усыпляет робота.
UNDO_SECONDS = 90.0


class Voice:
    """Речь робота: одна реплика за раз, и микрофон молчит, пока она звучит.

    Микрофон глушится не «на всякий случай»: аппаратного эхоподавления нет,
    робот слышит сам себя и отвечает сам себе — проверено на живом роботе.
    Замок нужен потому, что таймер срабатывает в своём потоке и может
    заговорить посреди ответа: в режиме local это два piper на одну звуковую
    карту, в режиме browser — две реплики в очереди пульта.
    """

    def __init__(self, speaker: Speaker, listener: Listener) -> None:
        self.speaker = speaker
        self.listener = listener
        self._lock = threading.RLock()
        # Куда показать услышанное. Пульт рисует это субтитрами: без экрана и
        # без SSH иначе не понять, расслышал робот или нет.
        self.on_heard: Callable[[str, str], None] | None = None

    @contextlib.contextmanager
    def quiet(self):
        """Заглушает микрофон на время звучания реплики."""
        self.listener.mute()
        try:
            yield
        finally:
            self.listener.unmute()

    @contextlib.contextmanager
    def hold(self):
        """Занимает голос, не трогая микрофон.

        Для потокового ответа: пока модель думает, робот молчит и должен
        слышать «стоп» — особенно если модель уже успела вызвать drive.
        """
        with self._lock:
            yield

    def say(self, text: str, *, loud: bool = False) -> int:
        """Говорит и возвращает, сколько слушателей реально услышали."""
        if not text:
            return 0
        with self._lock, self.quiet():
            return self.speaker.say(text, loud=loud)

    def heard(self, text: str) -> None:
        self._show("heard", text)

    def status(self, text: str) -> None:
        """Состояние робота, а не реплика человека: рисуется иначе."""
        self._show("status", text)

    def _show(self, kind: str, text: str) -> None:
        if self.on_heard is not None and text:
            try:
                self.on_heard(kind, text)
            except Exception:
                log.debug("не смог показать %s в пульте", kind, exc_info=True)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _clean_token(token: str) -> str:
    return token.lower().replace("ё", "е").strip(" ,.!?…—-\"'()")


def _sounds_like_wake(token: str, wake_words: tuple[str, ...], ratio: float) -> bool:
    """Похоже ли слово на имя робота.

    Whisper пишет имя по-разному — «Кузя», «Кузь», «Куся», — поэтому сравниваем
    нестрого. Порог держим высоким: имя короткое, а на четырёх буквах послабление
    начинает ловить всё подряд.
    """
    if not token:
        return False
    for wake in wake_words:
        if token == wake or token.startswith(wake):
            return True
        if difflib.SequenceMatcher(None, token, wake).ratio() >= ratio:
            return True
    return False


def _strip_wake_word(text: str, wake_words: tuple[str, ...],
                     ratio: float = 0.8) -> str | None:
    """Возвращает текст без имени, либо None, если робота не звали.

    Имя ищется не только в начале: «Кузя, вперёд» и «а ну-ка Кузя вперёд»
    одинаково законны. Дальше первых трёх слов не смотрим — иначе случайное
    упоминание в середине разговора будет принято за обращение.
    """
    if not wake_words:
        return text

    tokens = text.split()
    for i, token in enumerate(tokens[:3]):
        if _sounds_like_wake(_clean_token(token), wake_words, ratio):
            rest = tokens[:i] + tokens[i + 1:]
            # Слова перед именем — обращение («эй», «слушай»), а не команда.
            if all(_clean_token(t) in _FILLERS for t in tokens[:i]):
                rest = tokens[i + 1:]
            return " ".join(rest).strip(" ,.!?…—-")
    return None


def _is_looped(text: str) -> bool:
    """Зациклился ли Whisper: «сантиметров, сантиметров, сантиметров…».

    Живая речь так не выглядит: даже «да да да» — это три слова, а не сорок.
    Страховка на случай, если параметры против повторов не сработают.
    """
    words = re.findall(r"\w+", text.lower())
    if len(words) < 8:
        return False
    top = max(collections.Counter(words).values())
    return top / len(words) > 0.4


def _is_junk(text: str) -> bool:
    stripped = text.strip()
    if _JUNK.match(stripped) or not re.search(r"[а-яa-z]", stripped, re.I):
        return True
    if _is_looped(stripped):
        log.info("whisper зациклился, отбрасываю")
        return True
    # Титры переводчиков осмысленны по форме, но появляются только на тишине.
    return bool(_HALLUCINATION.search(stripped)) and len(stripped) < 120


class Watchdog:
    """Присматривает за тем, о чём человек узнаёт последним.

    Заряд: полоску в пульте видно, только если пульт открыт и на него смотрят.
    Робот ездит по квартире, а садится всегда не вовремя, поэтому о разряде он
    сообщает голосом — но ровно по одному разу на порог, иначе это нытьё.

    Микрофон: если телефон уснул или Wi-Fi моргнул, робот молчит, и снаружи
    это неотличимо от «обиделся». Состояние уходит в пульт строкой.
    """

    LEVELS = ((11.1, "Батарея меньше половины."),
              (10.8, "Батарея садится, пора на зарядку."),
              (CUTOFF_VOLT, "Батарея почти пустая, ехать я больше не буду."))

    def __init__(self, ros, voice: Voice, listener: Listener,
                 period: float = 30.0) -> None:
        self.ros = ros
        self.voice = voice
        self.listener = listener
        self.period = period
        self._said: set[float] = set()
        self._mic_was: bool | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.period):
            try:
                self._battery()
                self._mic()
            except Exception:
                log.exception("наблюдатель споткнулся")

    def _battery(self) -> None:
        volt = self.ros.voltage
        if volt is None:
            return
        # Зарядили — снова можно предупреждать.
        self._said = {v for v in self._said if volt <= v}
        for level, phrase in self.LEVELS:
            if volt <= level and level not in self._said:
                self._said.add(level)
                log.info("батарея %.1f В — предупреждаю", volt)
                self.voice.say(phrase)
                break

    def _mic(self) -> None:
        online = self.listener.pump.online
        if online == self._mic_was:
            return
        self._mic_was = online
        log.info("микрофон %s", "на связи" if online else "не отвечает")
        self.voice.status("микрофон на связи" if online
                          else "микрофон не отвечает — проверь телефон")


def _post_heard(speak_endpoint: str, kind: str, text: str) -> None:
    """Показать в пульте, что робот расслышал. Ошибку глушим: это подсказка."""
    req = urllib.request.Request(
        f"{speak_endpoint}/{kind}", data=text.encode(), method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        urllib.request.urlopen(req, timeout=3).close()
    except (urllib.error.URLError, OSError):
        pass


def main() -> None:
    _setup_logging()

    cfg = Config()
    cfg.check()
    log.info("модель %s через %s, effort %s, микрофон %s, динамик %s",
             cfg.model, cfg.api_base or "api.anthropic.com",
             cfg.effort or "не задан", cfg.audio_source, cfg.audio_out)
    # Часы робота видны сразу: от них зависят будильники, напоминания и тихие
    # часы. На свежем образе время обычно в UTC, и «разбуди в семь» окажется
    # на три часа мимо — а понять это по поведению трудно.
    log.info("часы робота: %s, тихие часы %s",
             datetime.now().strftime("%d.%m %H:%M"),
             cfg.quiet_hours or "выключены")

    # Настроенное голосом переживает перезапуск: автообновление случается
    # каждые две минуты, и громкость не должна возвращаться сама.
    state = State(cfg.data_dir / "settings.json")
    speaker = Speaker(cfg.piper_model_path,
                      audio_out=cfg.audio_out, web_endpoint=cfg.web_endpoint,
                      volume=float(state.get("volume", cfg.volume)))
    speaker.on_volume = lambda level: state.set("volume", level)
    speaker.quiet_now = cfg.is_quiet_now
    speaker.quiet_volume = cfg.quiet_volume

    ros = Ros(cfg.rosbridge_url)
    ros.start()
    if not ros.wait_connected(timeout=15):
        log.warning("rosbridge не ответил — еду вслепую, команды движения не пройдут")

    # Пока робот едет, автообновление не должно перезапускать сервис.
    busy = BusyFlag(ros)
    busy.start()

    recognizer = Recognizer(cfg.whisper_model, cfg.language,
                            beam_size=cfg.whisper_beam)

    listener = Listener(
        make_source(cfg.audio_source, cfg.phone_url, cfg.sample_rate),
        sample_rate=cfg.sample_rate,
        vad_level=cfg.vad_level,
        silence_ms=cfg.silence_ms,
        min_speech_ms=cfg.min_speech_ms,
        max_speech_ms=cfg.max_speech_ms,
    )
    voice = Voice(speaker, listener)
    voice.on_heard = lambda kind, text: _post_heard(cfg.web_endpoint, kind, text)

    # Таймеры и список переживают перезапуск: автообновление может случиться в
    # любой момент, а таймер на духовку от этого пропадать не должен.
    timers = Timers(announce=voice.say, store=cfg.data_dir / "timers.json")
    notes = Notes(cfg.data_dir / "notes.json")
    addressed = Addressed()
    tools = build_tools(ros, timers, speaker=speaker, notes=notes,
                        place=(cfg.lat, cfg.lon), addressed=addressed)
    brain = Brain(cfg, tools)

    # Робот сам скажет, что садится и что оглох: смотреть на пульт некому.
    watch = Watchdog(ros, voice, listener)
    watch.start()

    def shutdown(_sig, _frm) -> None:
        # Выход именно через sys.exit: главный цикл висит в чтении звука и
        # флаг увидел бы только после следующей фразы, то есть никогда.
        log.info("останавливаюсь")
        timers.cancel_all()
        ros.stop_motion()
        busy.stop()
        watch.stop()
        ros.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    greeting = cfg.wake_words[0].capitalize() if cfg.wake_words else "Робот"
    since_greet = time.time() - float(state.get("greeted_at", 0) or 0)
    if cfg.is_quiet_now():
        # Ночью автообновление перезапускает сервис так же, как днём, и
        # «Кузя на связи» в три часа — ровно то, за что робота выключают.
        log.info("тихие часы — здороваться не буду")
    elif since_greet < GREET_SILENCE:
        # Автообновление перезапускает сервис каждые две минуты, если в
        # репозитории что-то поменялось. Здороваться на каждый перезапуск —
        # это здороваться весь день.
        log.info("недавно уже здоровался (%.0f мин назад) — молчу", since_greet / 60)
    else:
        voice.say(f"{greeting} на связи.")
        state.set("greeted_at", time.time())
    timers.restore()

    while True:
        try:
            _listen_loop(cfg, listener, recognizer, brain, voice, tools, addressed)
        except KeyboardInterrupt:
            shutdown(None, None)
        except Exception:
            log.exception("сбой в цикле, перезапускаю через 5 с")
            ros.stop_motion()
            time.sleep(5)


def _dump_audio(wav: bytes, text: str) -> None:
    """Сохраняет услышанное, чтобы можно было послушать ушами.

    Когда Whisper выдаёт бессмыслицу, вопрос всегда один: он плохо распознаёт
    или ему приносят шум? Ответить можно только послушав. Включается
    переменной ROBOT_DEBUG_AUDIO=1.
    """
    folder = Path("/tmp/robot-audio")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # Оставляем последние два десятка, флешку забивать незачем.
        old = sorted(folder.glob("*.wav"))[:-20]
        for f in old:
            f.unlink(missing_ok=True)
        name = time.strftime("%H%M%S") + "-" + re.sub(r"\W+", "_", text[:30] or "пусто")
        (folder / f"{name}.wav").write_bytes(wav)
    except OSError as e:
        log.warning("не смог сохранить запись: %s", e)


def _is_sleep_word(command: str, sleep_words: tuple[str, ...]) -> bool:
    """Отпустили ли робота. Сравниваем фразу целиком, а не по вхождению:
    «спасибо» — прощание, «спасибо что напомнил про таймер» — нет."""
    cleaned = re.sub(r"[^\w\s]", "", command.lower().replace("ё", "е")).strip()
    return cleaned in sleep_words


class Addressed:
    """Звали ли робота по имени в текущей реплике.

    Инструменты движения спрашивают об этом сами: «поезжай на кухню» правилами
    не разбирается и уходит модели, а такие формулировки как раз и звучат из
    телевизора. Проверка в одном месте — в tools.py — закрывает оба пути.
    """

    def __init__(self) -> None:
        self.by_name = True

    def __call__(self) -> bool:
        return self.by_name


def _listen_loop(cfg: Config, listener: Listener, recognizer: Recognizer,
                 brain: Brain, voice: Voice, tools: list,
                 addressed: Addressed) -> None:
    by_name = {t.name: t for t in tools}
    name = cfg.wake_words[0].capitalize() if cfg.wake_words else "робот"
    log.info("слушаю, имя — %s, разговор держится %.0f с после ответа",
             name, cfg.session_seconds)

    # Робот слушает всегда, но реагирует только после имени. Дальше остаётся
    # в разговоре и отвечает без имени, пока не замолчат.
    awake_until = 0.0

    debug_audio = os.environ.get("ROBOT_DEBUG_AUDIO") == "1"
    if debug_audio:
        log.info("записи услышанного складываю в /tmp/robot-audio")

    # Момент последней реплики живёт в brain: цикл может перезапуститься после
    # сбоя, а разговор от этого не становится свежим.
    last_talk = brain.last_talk
    deaf = False        # прошлая фраза не разобралась — второй раз не ноем
    pending = Pending()
    undo = Undo()

    for wav in listener.utterances():
        text = recognizer.transcribe(wav)
        if debug_audio:
            _dump_audio(wav, text)
        if _is_junk(text):
            # В разговоре молчать нельзя: человек не понимает, услышали его
            # или нет, и начинает повторять всё громче. Но говорить это на
            # каждый шорох тоже нельзя — иначе шум на кухне зациклит робота
            # на одной фразе. Поэтому только на первый неразобранный подряд.
            if time.monotonic() < awake_until and not deaf:
                voice.say("Не расслышал.")
                awake_until = time.monotonic() + cfg.session_seconds
            deaf = True
            continue
        deaf = False

        # Долгая тишина — значит это уже другой разговор, а не продолжение.
        if last_talk and time.monotonic() - last_talk > FORGET_SECONDS:
            log.info("прошлый разговор давно закончился, забываю его")
            brain.reset()

        awake = time.monotonic() < awake_until
        command = _strip_wake_word(text, cfg.wake_words, cfg.wake_ratio)
        # Инструменты движения смотрят на этот признак: ехать можно только по
        # имени, кто бы ни попросил — правило или модель.
        addressed.by_name = command is not None or not cfg.motion_needs_name

        if command is None:
            if not awake:
                log.info("не мне (%r)", text)
                continue
            # В разговоре имя не нужно. Но окно открыто двадцать секунд, и в
            # него попадает всё, что звучит в комнате: телевизор, разговор с
            # другим человеком. Длинную фразу без имени считаем не своей —
            # иначе робот платит модели за чужие реплики.
            command = text.strip()
            if len(command.split()) > IN_SESSION_WORDS and intents.parse(command) is None:
                log.info("в окне, но длинно и не команда — не моё (%r)", command)
                continue
        elif not awake:
            log.info("проснулся по имени")

        voice.heard(command or text)

        if not command:
            # Позвали и замолчали — откликаемся и ждём продолжения.
            voice.say("Да?")
            awake_until = time.monotonic() + cfg.session_seconds
            last_talk = brain.last_talk = time.monotonic()
            continue

        log.info("человек: %s", command)

        # Простые команды разбираем правилами: мгновенно, бесплатно и без
        # риска, что модель поймёт «влево» как «вправо». Всё остальное — модели.
        match = intents.parse(command)

        # «Стоп» не должен ни за чем стоять в очереди. Раньше «отбой» на ходу
        # усыплял робота, а «стой» во время переспроса про таймеры считалось
        # ответом на вопрос — робот вежливо отвечал и продолжал ехать.
        if match is not None and match.tool == "stop":
            pending.clear()
            _run_direct(by_name["stop"], {}, voice)
            awake_until = time.monotonic() + cfg.session_seconds
            last_talk = brain.last_talk = time.monotonic()
            continue

        # «Отмена» сразу после ошибочно поставленного таймера — это просьба
        # его снять, а не попрощаться. Так это понимают все ассистенты, и так
        # это понимает человек: он только что услышал «Поставил таймер».
        if awake and undo.take(command, by_name, voice):
            awake_until = time.monotonic() + cfg.session_seconds
            last_talk = brain.last_talk = time.monotonic()
            continue

        if awake and _is_sleep_word(command, cfg.sleep_words):
            log.info("отпустили, засыпаю")
            awake_until = 0.0
            pending.clear()
            brain.reset()   # разговор закончен, тянуть его в следующий незачем
            voice.say("Ага, зови.")
            continue

        answered = pending.consume(command, match, voice)
        if answered:
            pass                    # это был ответ на вопрос робота
        elif match is not None and match.tool in by_name:
            _run_direct(by_name[match.tool], match.args, voice, pending)
            undo.remember(match.tool, match.args)
        else:
            # Помечаем явно: по этим строкам в логе видно, каких формулировок
            # не хватает правилам. Это лучший источник для их пополнения —
            # выборка под конкретного человека, а не общий корпус.
            log.info("правилами не разобрал, спрашиваю модель")
            _respond(command, brain, voice)

        # Окно отсчитываем от конца ответа, а не от начала: иначе длинная
        # реплика робота съедала бы всё время, отведённое на продолжение.
        awake_until = time.monotonic() + cfg.session_seconds
        last_talk = brain.last_talk = time.monotonic()


class Undo:
    """Последнее сделанное — чтобы «отмена» отменяла его, а не усыпляла робота.

    Whisper иногда слышит «поставь таймер на двадцать минут» там, где было
    «на двенадцать». Человек говорит «отмена» — и раньше робот просто прощался,
    а неверный таймер оставался идти.
    """

    # Что можно отменить и чем.
    UNDOABLE = {
        "set_timer": ("cancel_timer", "label"),
        "set_alarm": ("cancel_timer", "label"),
        "set_reminder": ("cancel_timer", "label"),
        "notes_add": ("notes_remove", "item"),
    }

    def __init__(self) -> None:
        self.tool = ""
        self.args: dict = {}
        self.at = 0.0

    def remember(self, tool: str, args: dict) -> None:
        if tool in self.UNDOABLE:
            self.tool, self.args, self.at = tool, dict(args), time.monotonic()
        else:
            self.tool = ""

    def take(self, command: str, by_name: dict, voice: Voice) -> bool:
        """Отменяет последнее, если просьба похожа на отмену и она свежая."""
        if not self.tool or time.monotonic() - self.at > UNDO_SECONDS:
            return False
        plain = re.sub(r"[^\w\s]", "", command.lower().replace("ё", "е")).strip()
        if plain not in ("отмена", "отмени", "отменить", "забудь", "не надо",
                         "неважно", "не важно"):
            return False

        name, field = self.UNDOABLE[self.tool]
        tool = by_name.get(name)
        self.tool = ""
        if tool is None:
            return False
        # Название таймера робот знает своё; будильник зовётся «будильник».
        value = self.args.get(field) or self.args.get("label") or (
            "будильник" if name == "cancel_timer" else "")
        log.info("отменяю последнее: %s", value or "без названия")
        voice.say(tool({field: value} if value else {}))
        return True


class Pending:
    """Вопрос, который робот задал сам, и ответ на него.

    Инструменты умеют переспрашивать: «Таймера чай нет. Есть безымянный и
    лапша. Какой снять?» Без этой памяти следующая фраза человека уходила
    модели, которая про заданный вопрос ничего не знает, — и разговор
    обрывался на полуслове.
    """

    LIFETIME = 20.0
    YES = {"да", "давай", "ага", "конечно", "верно", "точно", "отменяй",
           "снимай", "угу", "да давай", "именно"}
    NO = {"нет", "не надо", "не нужно", "отставить", "погоди", "стой"}

    def __init__(self) -> None:
        self.tool = None
        self.field = ""
        self.confirm = False
        self.args: dict = {}
        self.until = 0.0

    def ask(self, tool, field: str = "label", *, confirm: bool = False,
            args: dict | None = None) -> None:
        self.tool, self.field, self.confirm = tool, field, confirm
        self.args = dict(args or {})
        self.until = time.monotonic() + self.LIFETIME

    def clear(self) -> None:
        self.tool = None
        self.until = 0.0

    def consume(self, command: str, match, voice: Voice) -> bool:
        """Ответ ли это на заданный вопрос. True — обработали сами."""
        if self.tool is None or time.monotonic() > self.until:
            self.clear()
            return False

        plain = re.sub(r"[^\w\s]", "", command.lower().replace("ё", "е")).strip()
        tool, field, confirm, args = self.tool, self.field, self.confirm, self.args
        self.clear()

        if confirm:
            if plain in self.YES:
                voice.say(tool(args))
                return True
            if plain in self.NO or match is None:
                voice.say("Хорошо, не буду.")
                return True
            return False

        # Ответ на «какой именно?» — это короткое название, а не новая команда.
        if match is not None or len(plain.split()) > 3:
            return False
        voice.say(tool({**args, field: plain}))
        return True


# Вопрос робота слышно по последнему знаку: инструменты заканчивают переспрос
# вопросительным знаком, а обычный ответ — точкой.
_ASKS_LABEL = ("Какой снять?", "Какой поставить на паузу?", "Какой продолжить?")


def _run_direct(tool, args: dict, voice: Voice, pending: Pending | None = None) -> None:
    """Выполняет команду, разобранную правилом, минуя модель."""
    # Инструмент вызываем до заглушения микрофона: движение стартует сразу, и
    # эти доли секунды робот ещё слышит комнату.
    answer = tool(args)
    if pending is not None and answer.endswith(_ASKS_LABEL):
        pending.ask(tool, "label", args=args)
    elif (pending is not None and answer.endswith("?")
            and "confirmed" in tool.input_schema.get("properties", {})):
        pending.ask(tool, confirm=True, args={**args, "confirmed": True})
    voice.say(answer)   # say сам пишет реплику в лог


def _failure_phrase(error: Exception) -> str:
    """Почему не вышло ответить — так, чтобы человек понял, что делать.

    Раньше на всё было одно «Что-то пошло не так, повтори»: человек повторял
    и получал то же самое. При телефоне-микрофоне и двойном NAT отвалившийся
    интернет — обычное дело, и сказать об этом честно полезнее.
    """
    name = type(error).__name__
    if "Connection" in name or "Timeout" in name:
        return ("Сейчас я без интернета. Таймеры, время, список и езда "
                "работают, а поговорить не выйдет.")
    if "Authentication" in name or "PermissionDenied" in name:
        return "Модель меня не пускает — похоже, ключ. Проверь настройки."
    if "RateLimit" in name:
        return "Модель занята, попробуй через минуту."
    return "Что-то пошло не так, повтори."


def _respond(command: str, brain: Brain, voice: Voice) -> None:
    """Отвечает вслух, проговаривая предложения по мере генерации."""
    buffer = SentenceBuffer()

    # Микрофон глушим не на весь ход, а с первого произнесённого предложения:
    # пока модель думает, робот молчит — и должен слышать «стоп», тем более
    # что модель могла уже вызвать drive и робот в этот момент едет.
    #
    # Синтез открываем уже под замком: в режиме local это отдельный процесс
    # piper на общую звуковую карту, и открывать его раньше, чем занят голос,
    # значит однажды наложиться на объявление таймера.
    with voice.hold(), contextlib.ExitStack() as stack:
        speech = voice.speaker.stream()
        speaking = False

        def start_speaking() -> None:
            nonlocal speaking
            if not speaking:
                stack.enter_context(voice.quiet())
                speaking = True

        def on_text(chunk: str) -> None:
            for sentence in buffer.push(chunk):
                log.info("робот: %s", sentence)
                start_speaking()
                if speech:
                    speech.feed(sentence)

        try:
            brain.reply(command, on_text)
            tail = buffer.flush()
            if tail:
                log.info("робот: %s", tail)
                start_speaking()
                if speech:
                    speech.feed(tail)
        except Exception as e:
            log.exception("не смог ответить")
            start_speaking()
            if speech:
                speech.feed(_failure_phrase(e))
        finally:
            if speech:
                try:
                    speech.close()
                except Exception:
                    log.exception("сбой при озвучивании")
