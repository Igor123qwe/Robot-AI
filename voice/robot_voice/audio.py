"""Захват звука и нарезка на фразы по детектору речи.

Два источника:
  local — микрофон, воткнутый в RDK X5 (после приезда ReSpeaker Lite);
  phone — старый Android с приложением IP Webcam, отдаёт /audio.wav.

Режим строго половинного дуплекса: пока робот говорит, микрофон заглушён. Программный
AEC поверх Wi-Fi с плавающей задержкой всё равно не работает, так что barge-in
появится только вместе с аппаратным эхоподавлением ReSpeaker.
"""

from __future__ import annotations

import io
import logging
import struct
import threading
import wave
from collections import deque
from typing import Iterator

import numpy as np
import webrtcvad

log = logging.getLogger(__name__)

FRAME_MS = 20  # webrtcvad принимает только 10, 20 или 30 мс


# --------------------------------------------------------------------------
# Источники
# --------------------------------------------------------------------------
class LocalSource:
    """Микрофон через sounddevice."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.frame_len = sample_rate * FRAME_MS // 1000

    def frames(self) -> Iterator[np.ndarray]:
        import sounddevice as sd

        with sd.InputStream(samplerate=self.sample_rate, channels=1,
                            dtype="int16", blocksize=self.frame_len) as stream:
            while True:
                data, overflowed = stream.read(self.frame_len)
                if overflowed:
                    log.debug("аудио: переполнение буфера")
                yield data.reshape(-1).copy()


class PhoneSource:
    """Микрофон телефона: HTTP-поток WAV от IP Webcam."""

    def __init__(self, base_url: str, sample_rate: int) -> None:
        self.url = base_url.rstrip("/") + "/audio.wav"
        self.sample_rate = sample_rate
        self.frame_len = sample_rate * FRAME_MS // 1000

    def frames(self) -> Iterator[np.ndarray]:
        import requests

        log.info("аудио: подключаюсь к %s", self.url)
        resp = requests.get(self.url, stream=True, timeout=(5, 30))
        resp.raise_for_status()

        raw = resp.raw
        src_rate, channels = _read_wav_header(raw)
        log.info("аудио: поток %d Гц, каналов %d", src_rate, channels)

        # Читаем блоками, приводим к 16 кГц моно, режем на кадры по 20 мс.
        tail = np.empty(0, dtype=np.int16)
        chunk_bytes = src_rate * channels * 2 // 5  # ~200 мс
        while True:
            buf = raw.read(chunk_bytes)
            if not buf:
                raise ConnectionError("аудиопоток телефона оборвался")
            samples = np.frombuffer(buf[: len(buf) // 2 * 2], dtype="<i2")
            if channels > 1:
                samples = samples.reshape(-1, channels)[:, 0]
            if src_rate != self.sample_rate:
                samples = _resample(samples, src_rate, self.sample_rate)

            tail = np.concatenate([tail, samples])
            n = len(tail) // self.frame_len
            for i in range(n):
                yield tail[i * self.frame_len:(i + 1) * self.frame_len]
            tail = tail[n * self.frame_len:]


def _read_wav_header(stream) -> tuple[int, int]:
    """Разбирает RIFF-заголовок и оставляет поток на первом сэмпле."""
    riff = stream.read(12)
    if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise ValueError("это не WAV-поток")
    rate = channels = 0
    while True:
        head = stream.read(8)
        if len(head) < 8:
            raise ValueError("WAV-заголовок оборвался")
        chunk_id, size = struct.unpack("<4sI", head)
        if chunk_id == b"fmt ":
            fmt = stream.read(size)
            channels = struct.unpack_from("<H", fmt, 2)[0]
            rate = struct.unpack_from("<I", fmt, 4)[0]
        elif chunk_id == b"data":
            return rate, channels
        else:
            stream.read(size)


def _resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Линейная передискретизация. Для речи 16 кГц её качества достаточно."""
    if len(samples) == 0:
        return samples
    n_out = int(len(samples) * dst / src)
    idx = np.linspace(0, len(samples) - 1, n_out)
    return np.interp(idx, np.arange(len(samples)), samples).astype(np.int16)


def make_source(audio_source: str, phone_url: str, sample_rate: int):
    if audio_source == "local":
        return LocalSource(sample_rate)
    return PhoneSource(phone_url, sample_rate)


# --------------------------------------------------------------------------
# Нарезка на фразы
# --------------------------------------------------------------------------
class Listener:
    """Выдаёт по одной законченной фразе (WAV-байты) за раз."""

    def __init__(self, source, *, sample_rate: int, vad_level: int,
                 silence_ms: int, min_speech_ms: int) -> None:
        self.source = source
        self.sample_rate = sample_rate
        self.vad = webrtcvad.Vad(vad_level)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        # Небольшой предбуфер, чтобы не отрезать начало слова.
        self.preroll = deque(maxlen=max(1, 250 // FRAME_MS))
        self._muted = threading.Event()

    # Пока робот говорит — не слушаем: иначе он расслышит сам себя.
    def mute(self) -> None:
        self._muted.set()

    def unmute(self) -> None:
        self._muted.clear()
        self.preroll.clear()

    def utterances(self) -> Iterator[bytes]:
        speech: list[np.ndarray] = []
        silence = 0
        talking = False

        for frame in self.source.frames():
            if self._muted.is_set():
                speech.clear()
                silence = 0
                talking = False
                continue

            is_speech = self.vad.is_speech(frame.tobytes(), self.sample_rate)

            if not talking:
                self.preroll.append(frame)
                if is_speech:
                    talking = True
                    speech = list(self.preroll)
                    silence = 0
                continue

            speech.append(frame)
            silence = 0 if is_speech else silence + 1

            if silence >= self.silence_frames:
                talking = False
                voiced = len(speech) - silence
                payload, speech = speech, []
                self.preroll.clear()
                if voiced >= self.min_speech_frames:
                    yield to_wav(np.concatenate(payload), self.sample_rate)
                else:
                    log.debug("аудио: слишком короткий фрагмент, пропускаю")


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()
