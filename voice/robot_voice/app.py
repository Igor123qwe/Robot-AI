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
from pathlib import Path

from . import intents
from .audio import Listener, make_source
from .brain import Brain
from .busyflag import BusyFlag
from .config import Config
from .ros import Ros
from .stt import Recognizer
from .tools import Timers, build_tools
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

    def say(self, text: str) -> None:
        with self._lock, self.quiet():
            self.speaker.say(text)


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


def main() -> None:
    _setup_logging()

    cfg = Config()
    cfg.check()
    log.info("модель %s через %s, effort %s, микрофон %s, динамик %s",
             cfg.model, cfg.api_base or "api.anthropic.com",
             cfg.effort or "не задан", cfg.audio_source, cfg.audio_out)

    speaker = Speaker(cfg.piper_model_path,
                      audio_out=cfg.audio_out, web_endpoint=cfg.web_endpoint)
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

    # Таймеры переживают перезапуск: автообновление может случиться в любой
    # момент, а таймер на духовку от этого пропадать не должен.
    timers = Timers(announce=voice.say, store=Path.home() / ".robot-ai" / "timers.json")
    tools = build_tools(ros, timers)
    brain = Brain(cfg, tools)

    stopping = False

    def shutdown(_sig, _frm) -> None:
        nonlocal stopping
        stopping = True
        log.info("останавливаюсь")
        timers.cancel_all()
        ros.stop_motion()
        busy.stop()
        ros.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    greeting = cfg.wake_words[0].capitalize() if cfg.wake_words else "Робот"
    voice.say(f"{greeting} на связи.")
    timers.restore()

    while not stopping:
        try:
            _listen_loop(cfg, listener, recognizer, brain, voice, tools)
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


def _listen_loop(cfg: Config, listener: Listener, recognizer: Recognizer,
                 brain: Brain, voice: Voice, tools: list) -> None:
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

    last_talk = 0.0

    for wav in listener.utterances():
        text = recognizer.transcribe(wav)
        if debug_audio:
            _dump_audio(wav, text)
        if _is_junk(text):
            continue

        # Долгая тишина — значит это уже другой разговор, а не продолжение.
        if last_talk and time.monotonic() - last_talk > FORGET_SECONDS:
            log.info("прошлый разговор давно закончился, забываю его")
            brain.reset()

        awake = time.monotonic() < awake_until
        command = _strip_wake_word(text, cfg.wake_words, cfg.wake_ratio)

        if command is None:
            if not awake:
                log.info("не мне (%r)", text)
                continue
            # В разговоре имя не нужно.
            command = text.strip()
        elif not awake:
            log.info("проснулся по имени")

        if not command:
            # Позвали и замолчали — откликаемся и ждём продолжения.
            voice.say("Да?")
            awake_until = time.monotonic() + cfg.session_seconds
            last_talk = time.monotonic()
            continue

        if awake and _is_sleep_word(command, cfg.sleep_words):
            log.info("отпустили, засыпаю")
            awake_until = 0.0
            brain.reset()   # разговор закончен, тянуть его в следующий незачем
            voice.say("Ага, зови.")
            continue

        log.info("человек: %s", command)

        # Простые команды разбираем правилами: мгновенно, бесплатно и без
        # риска, что модель поймёт «влево» как «вправо». Всё остальное — модели.
        match = intents.parse(command)
        if match is not None and match.tool in by_name:
            _run_direct(by_name[match.tool], match.args, voice)
        else:
            # Помечаем явно: по этим строкам в логе видно, каких формулировок
            # не хватает правилам. Это лучший источник для их пополнения —
            # выборка под конкретного человека, а не общий корпус.
            log.info("правилами не разобрал, спрашиваю модель")
            _respond(command, brain, voice)

        # Окно отсчитываем от конца ответа, а не от начала: иначе длинная
        # реплика робота съедала бы всё время, отведённое на продолжение.
        awake_until = time.monotonic() + cfg.session_seconds
        last_talk = awake_until


def _run_direct(tool, args: dict, voice: Voice) -> None:
    """Выполняет команду, разобранную правилом, минуя модель."""
    # Инструмент вызываем до заглушения микрофона: движение стартует сразу, и
    # эти доли секунды робот ещё слышит комнату.
    answer = tool(args)
    voice.say(answer)   # say сам пишет реплику в лог


def _respond(command: str, brain: Brain, voice: Voice) -> None:
    """Отвечает вслух, проговаривая предложения по мере генерации."""
    speech = voice.speaker.stream()
    buffer = SentenceBuffer()

    # Микрофон глушим не на весь ход, а с первого произнесённого предложения:
    # пока модель думает, робот молчит — и должен слышать «стоп», тем более
    # что модель могла уже вызвать drive и робот в этот момент едет.
    with voice.hold(), contextlib.ExitStack() as stack:
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
        except Exception:
            log.exception("не смог ответить")
            start_speaking()
            if speech:
                speech.feed("Что-то пошло не так, повтори.")
        finally:
            if speech:
                try:
                    speech.close()
                except Exception:
                    log.exception("сбой при озвучивании")
