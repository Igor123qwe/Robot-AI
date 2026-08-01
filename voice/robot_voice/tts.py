"""Синтез речи — Piper. Куда его выводить, зависит от того, что подключено.

  local   — сразу в aplay. Piper запускается один раз на реплику, читает stdin
            построчно и отдаёт сырой звук, поэтому робот начинает говорить, не
            дожидаясь, пока Claude допишет ответ. Так будет, когда приедет
            динамик SOTAMIA.
  browser — реплика собирается в WAV и уходит в веб-сервер робота, а играет её
            браузер с открытым пультом. Временно, пока своего динамика нет:
            IP Webcam умеет отдавать звук, но не принимать.

В режиме browser синтез идёт целиком на реплику, а не по предложениям: иначе
Piper пришлось бы поднимать на каждую фразу. Ответы у нас в одно-два
предложения, так что задержка невелика.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

log = logging.getLogger(__name__)

# Конец предложения: точка/вопрос/восклицание, за которыми пробел или конец строки.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


class Speech:
    """Одна реплика робота в динамик. Предложения докидываются по мере генерации."""

    def __init__(self, piper_cmd: list[str], sample_rate: int) -> None:
        self._piper = subprocess.Popen(
            piper_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._aplay = subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
             "-r", str(sample_rate), "-"],
            stdin=self._piper.stdout, stderr=subprocess.DEVNULL,
        )
        # Дескриптор нужен только aplay — иначе он не увидит EOF.
        self._piper.stdout.close()

    def feed(self, sentence: str) -> None:
        sentence = sentence.strip()
        if not sentence or self._piper.stdin is None:
            return
        try:
            self._piper.stdin.write((sentence + "\n").encode())
            self._piper.stdin.flush()
        except BrokenPipeError:
            log.warning("piper: процесс закрылся раньше времени")

    def close(self) -> None:
        """Дожидается, пока всё сказанное действительно прозвучит."""
        try:
            if self._piper.stdin is not None:
                self._piper.stdin.close()
        except BrokenPipeError:
            pass
        self._piper.wait(timeout=120)
        self._aplay.wait(timeout=120)


class WebSpeech:
    """Одна реплика робота в браузер: копим текст, в конце шлём готовый WAV."""

    def __init__(self, piper_cmd: list[str], sample_rate: int, endpoint: str) -> None:
        self.piper_cmd = piper_cmd
        self.sample_rate = sample_rate
        self.endpoint = endpoint
        self._sentences: list[str] = []

    def feed(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self._sentences.append(sentence)

    def close(self) -> None:
        if not self._sentences:
            return
        text = " ".join(self._sentences)
        self._sentences = []
        try:
            wav = self._synthesize(text)
        except (OSError, subprocess.SubprocessError):
            log.exception("piper не смог синтезировать реплику")
            return
        self._post(wav, text)

    def _synthesize(self, text: str) -> bytes:
        raw = subprocess.run(
            self.piper_cmd, input=text.encode(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=120, check=True,
        ).stdout
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(raw)
        return buf.getvalue()

    def _post(self, wav: bytes, text: str) -> None:
        req = urllib.request.Request(
            self.endpoint, data=wav, method="POST",
            headers={
                "Content-Type": "audio/wav",
                # Заголовки — только ASCII, поэтому текст экранируем.
                "X-Robot-Text": urllib.parse.quote(text),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("не смог отдать реплику пульту: %s", e)
            return
        if not info.get("listeners"):
            log.info("реплика отправлена, но пульт никто не смотрит — её не услышат")


class Speaker:
    def __init__(self, model_path: Path, *, audio_out: str = "local",
                 web_endpoint: str = "http://127.0.0.1:8000/speak") -> None:
        self.model_path = model_path
        self.audio_out = audio_out
        self.web_endpoint = web_endpoint
        self.enabled = True
        self.sample_rate = 22050

        self.piper = _find_piper()
        if self.piper is None:
            log.error("piper не установлен — робот будет молчать (текст в логе)")
            self.enabled = False
            return
        if not model_path.exists():
            log.error("нет голоса %s — робот будет молчать", model_path)
            self.enabled = False
            return

        cfg = model_path.with_suffix(".onnx.json")
        if cfg.exists():
            try:
                self.sample_rate = json.loads(cfg.read_text())["audio"]["sample_rate"]
            except (ValueError, KeyError):
                log.warning("не разобрал %s, беру 22050 Гц", cfg.name)

    def _cmd(self) -> list[str]:
        return [self.piper, "--model", str(self.model_path), "--output-raw"]

    def stream(self):
        """Начать реплику. Куда она пойдёт — решает audio_out."""
        if not self.enabled:
            return None
        if self.audio_out == "browser":
            return WebSpeech(self._cmd(), self.sample_rate, self.web_endpoint)
        try:
            return Speech(self._cmd(), self.sample_rate)
        except OSError:
            log.exception("не смог запустить piper/aplay")
            return None

    def say(self, text: str) -> None:
        """Синхронно проговорить готовый текст."""
        log.info("робот: %s", text)
        speech = self.stream()
        if speech is None:
            return
        for sentence in split_sentences(text):
            speech.feed(sentence)
        speech.close()


def _find_piper() -> str | None:
    """Ищет piper сначала в текущем venv, потом в PATH.

    Сервис запускается как .venv/bin/python, а venv/bin в PATH при этом нет —
    поэтому одного shutil.which недостаточно.
    """
    local = Path(sys.executable).with_name("piper")
    if local.exists():
        return str(local)
    return shutil.which("piper")


def split_sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENTENCE_END.split(text)) if s]


class SentenceBuffer:
    """Копит поток токенов и отдаёт законченные предложения."""

    def __init__(self) -> None:
        self._buf = ""

    def push(self, chunk: str) -> list[str]:
        self._buf += chunk
        parts = _SENTENCE_END.split(self._buf)
        if len(parts) == 1:
            return []
        self._buf = parts[-1]
        return [p.strip() for p in parts[:-1] if p.strip()]

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest
