"""Главный цикл: слушаем → распознаём → отвечаем → говорим."""

from __future__ import annotations

import difflib
import logging
import re
import signal
import sys
import time

from . import intents
from .audio import Listener, make_source
from .brain import Brain
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


def _sounds_like_wake(token: str, wake_words: tuple[str, ...]) -> bool:
    """Похоже ли слово на обращение к роботу.

    Whisper пишет имя по-разному — «робот», «Робот,», «роберт», «робут», —
    поэтому сравниваем нестрого. Порог 0.75 отсекает «работа» и «пробор»,
    но прощает одну перепутанную букву.
    """
    if not token:
        return False
    for wake in wake_words:
        if token == wake or token.startswith(wake):
            return True
        if difflib.SequenceMatcher(None, token, wake).ratio() >= 0.75:
            return True
    return False


def _strip_wake_word(text: str, wake_words: tuple[str, ...]) -> str | None:
    """Возвращает текст без обращения, либо None, если обращения не было."""
    if not wake_words:
        return text

    tokens = text.split()
    i = 0
    while i < len(tokens) and _clean_token(tokens[i]) in _FILLERS:
        i += 1
    if i >= len(tokens) or not _sounds_like_wake(_clean_token(tokens[i]), wake_words):
        return None
    return " ".join(tokens[i + 1:]).strip(" ,.!?…—-")


def _is_junk(text: str) -> bool:
    stripped = text.strip()
    if _JUNK.match(stripped) or not re.search(r"[а-яa-z]", stripped, re.I):
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

    timers = Timers(announce=speaker.say)
    tools = build_tools(ros, timers)
    brain = Brain(cfg, tools)
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

    stopping = False

    def shutdown(_sig, _frm) -> None:
        nonlocal stopping
        stopping = True
        log.info("останавливаюсь")
        timers.cancel_all()
        ros.stop_motion()
        ros.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    speaker.say("Я здесь. Позови по имени.")

    while not stopping:
        try:
            _listen_loop(cfg, listener, recognizer, brain, speaker, tools)
        except KeyboardInterrupt:
            shutdown(None, None)
        except Exception:
            log.exception("сбой в цикле, перезапускаю через 5 с")
            ros.stop_motion()
            time.sleep(5)


def _listen_loop(cfg: Config, listener: Listener, recognizer: Recognizer,
                 brain: Brain, speaker: Speaker, tools: list) -> None:
    by_name = {t.name: t for t in tools}
    log.info("слушаю")

    for wav in listener.utterances():
        text = recognizer.transcribe(wav)
        if _is_junk(text):
            continue

        command = _strip_wake_word(text, cfg.wake_words)
        if command is None:
            log.info("не мне (%r)", text)
            continue
        if not command:
            speaker.say("Слушаю.")
            continue

        log.info("человек: %s", command)

        # Простые команды разбираем правилами: мгновенно, бесплатно и без
        # риска, что модель поймёт «влево» как «вправо». Всё остальное — модели.
        match = intents.parse(command)
        if match is not None and match.tool in by_name:
            _run_direct(by_name[match.tool], match.args, speaker, listener)
        else:
            # Помечаем явно: по этим строкам в логе видно, каких формулировок
            # не хватает правилам. Это лучший источник для их пополнения —
            # выборка под конкретного человека, а не общий корпус.
            log.info("правилами не разобрал, спрашиваю модель")
            _respond(command, brain, speaker, listener)


def _run_direct(tool, args: dict, speaker: Speaker, listener: Listener) -> None:
    """Выполняет команду, разобранную правилом, минуя модель."""
    listener.mute()
    try:
        speaker.say(tool(args))   # say сам пишет реплику в лог
    finally:
        listener.unmute()


def _respond(command: str, brain: Brain, speaker: Speaker, listener: Listener) -> None:
    """Отвечает вслух, проговаривая предложения по мере генерации."""
    listener.mute()  # без аппаратного AEC робот иначе услышит сам себя
    speech = speaker.stream()
    buffer = SentenceBuffer()

    def on_text(chunk: str) -> None:
        for sentence in buffer.push(chunk):
            log.info("робот: %s", sentence)
            if speech:
                speech.feed(sentence)

    try:
        brain.reply(command, on_text)
        tail = buffer.flush()
        if tail:
            log.info("робот: %s", tail)
            if speech:
                speech.feed(tail)
    except Exception:
        log.exception("не смог ответить")
        if speech:
            speech.feed("Что-то пошло не так, повтори.")
    finally:
        if speech:
            try:
                speech.close()
            except Exception:
                log.exception("сбой при озвучивании")
        listener.unmute()
