"""Главный цикл: слушаем → распознаём → отвечаем → говорим."""

from __future__ import annotations

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

# Мусор, который whisper любит выдавать на тишине и шуме.
_JUNK = re.compile(r"^[\s.,!?…\-—\"'()]*$")
_HALLUCINATIONS = {
    "субтитры сделал dimatorzok",
    "продолжение следует...",
    "редактор субтитров а.синецкая корректор а.егорова",
}


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _strip_wake_word(text: str, wake_words: tuple[str, ...]) -> str | None:
    """Возвращает текст без обращения, либо None, если обращения не было."""
    if not wake_words:
        return text
    low = text.lower().lstrip(" ,.!?—-")
    for word in wake_words:
        if low.startswith(word):
            rest = low[len(word):].lstrip(" ,.!?—-")
            # Отдаём исходный регистр хвоста, а не приведённый к нижнему.
            return text[len(text) - len(rest):].strip() if rest else ""
    return None


def _is_junk(text: str) -> bool:
    return bool(_JUNK.match(text)) or text.strip().lower() in _HALLUCINATIONS


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
    recognizer = Recognizer(cfg.whisper_model, cfg.language)

    listener = Listener(
        make_source(cfg.audio_source, cfg.phone_url, cfg.sample_rate),
        sample_rate=cfg.sample_rate,
        vad_level=cfg.vad_level,
        silence_ms=cfg.silence_ms,
        min_speech_ms=cfg.min_speech_ms,
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
