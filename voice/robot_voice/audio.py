"""Захват звука и нарезка на фразы по детектору речи.

Три источника:
  local   — микрофон, воткнутый в RDK X5 (после приезда ReSpeaker Lite);
  phone   — старый Android с приложением IP Webcam, отдаёт /audio.wav;
  browser — микрофон компьютера: вкладка пульта шлёт звук на веб-сервер
            робота, а мы забираем его оттуда потоком. Удобно для отладки:
            наушники на голове, эха нет, телефон не нужен.

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
    """Передискретизация с подавлением зеркал.

    Голая интерполяция при 44.1 → 16 кГц заворачивает всё выше 8 кГц обратно
    в слышимый диапазон: шипящие превращаются в свист, и распознавание на этом
    спотыкается. Поэтому перед прореживанием усредняем скользящим окном —
    грубый, но честный ФНЧ, которого для речи достаточно.
    """
    if len(samples) == 0:
        return samples

    x = samples.astype(np.float32)
    if src > dst:
        window = max(1, int(round(src / dst)))
        if window > 1:
            kernel = np.ones(window, dtype=np.float32) / window
            x = np.convolve(x, kernel, mode="same")

    n_out = int(len(x) * dst / src)
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype(np.int16)


class BrowserSource:
    """Микрофон компьютера через вкладку пульта.

    Вкладка шлёт сырой PCM на веб-сервер робота (`POST /listen`), а мы
    забираем его непрерывным потоком с `GET /listen/stream`. Формат уже
    нужный — 16 кГц, моно, int16, — так что ни заголовков, ни
    передискретизации: браузер это делает лучше нас.
    """

    def __init__(self, web_url: str, sample_rate: int) -> None:
        self.url = web_url.rstrip("/") + "/listen/stream"
        self.sample_rate = sample_rate
        self.frame_len = sample_rate * FRAME_MS // 1000

    def frames(self) -> Iterator[np.ndarray]:
        import requests

        log.info("аудио: жду звук из браузера на %s", self.url)
        resp = requests.get(self.url, stream=True, timeout=(5, 30))
        resp.raise_for_status()

        tail = np.empty(0, dtype=np.int16)
        # Читаем ровно по кадру: с большим буфером тишина, которой сервер
        # держит соединение живым, копилась бы по несколько секунд.
        for buf in resp.iter_content(chunk_size=self.frame_len * 2):
            if not buf:
                continue
            samples = np.frombuffer(buf[: len(buf) // 2 * 2], dtype="<i2")
            tail = np.concatenate([tail, samples])
            n = len(tail) // self.frame_len
            for i in range(n):
                yield tail[i * self.frame_len:(i + 1) * self.frame_len]
            tail = tail[n * self.frame_len:]
        raise ConnectionError("поток из браузера оборвался")


def make_source(audio_source: str, phone_url: str, sample_rate: int,
                web_url: str = "http://127.0.0.1:8000"):
    if audio_source == "local":
        return LocalSource(sample_rate)
    if audio_source == "browser":
        return BrowserSource(web_url, sample_rate)
    return PhoneSource(phone_url, sample_rate)


# --------------------------------------------------------------------------
# Развязка чтения и обработки
# --------------------------------------------------------------------------
class Pump:
    """Читает источник в своём потоке, храня только свежий хвост звука.

    Без этого получается растущее отставание: пока Whisper думает пару секунд,
    телефон продолжает слать, данные копятся в сокете, и робот начинает
    отвечать на позавчерашние реплики. Здесь старые кадры просто выбрасываются —
    для диалога свежесть важнее полноты.
    """

    def __init__(self, source, keep_seconds: float = 3.0) -> None:
        self.source = source
        maxlen = max(10, int(keep_seconds * 1000 / FRAME_MS))
        self._frames: deque[np.ndarray] = deque(maxlen=maxlen)
        self._ready = threading.Condition()
        self._dropped = 0
        self._stop = False
        self._thread: threading.Thread | None = None
        # Живой ли сейчас источник — чтобы наверху было что показать человеку.
        self.online = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        with self._ready:
            self._ready.notify_all()

    def _run(self) -> None:
        """Читает источник, переподключаясь сколько понадобится.

        Обрыв звука — это норма, а не аварийная ситуация: телефон уходит в сон,
        Wi-Fi моргает, IP Webcam перезапускается. Раньше первая же такая ошибка
        пробрасывалась наверх и робот глох до ручного `systemctl restart` —
        поток пересоздавался, но старая ошибка так и оставалась записанной.
        Теперь переподключение живёт здесь, и наверх ничего не летит.
        """
        delay = 1.0
        while not self._stop:
            try:
                for frame in self.source.frames():
                    if self._stop:
                        return
                    if not self.online:
                        log.info("аудио: источник на связи")
                        self.online = True
                    delay = 1.0
                    with self._ready:
                        if len(self._frames) == self._frames.maxlen:
                            self._dropped += 1
                            # Каждые 500 кадров — это 10 секунд выброшенного
                            # звука. Обычно значит, что Whisper не успевает:
                            # рядом говорит телевизор и режется по 20 секунд.
                            if self._dropped % 500 == 0:
                                log.warning("аудио: не успеваю разбирать, "
                                            "выброшено %d кадров", self._dropped)
                        self._frames.append(frame)
                        self._ready.notify()
            except Exception as e:      # noqa: BLE001 — обрыв не повод умирать
                if self._stop:
                    return
                if self.online:
                    log.warning("аудио: источник пропал (%s), переподключаюсь", e)
                else:
                    log.debug("аудио: источник не отвечает (%s)", e)
                self.online = False
            # Паузу наращиваем, чтобы не долбить выключенный телефон каждую
            # секунду, но и не спать полминуты, когда он вернулся.
            with self._ready:
                self._ready.wait(timeout=delay)
            delay = min(10.0, delay * 2)

    def frames(self):
        while not self._stop:
            with self._ready:
                while not self._frames and not self._stop:
                    self._ready.wait(timeout=1.0)
                if not self._frames:
                    continue
                frame = self._frames.popleft()
            yield frame

    def drop_pending(self) -> None:
        """Выбросить накопленное — например, всё, что робот наговорил сам."""
        with self._ready:
            self._frames.clear()


# --------------------------------------------------------------------------
# Нарезка на фразы
# --------------------------------------------------------------------------
class Listener:
    """Выдаёт по одной законченной фразе (WAV-байты) за раз."""

    def __init__(self, source, *, sample_rate: int, vad_level: int,
                 silence_ms: int, min_speech_ms: int, max_speech_ms: int = 20000,
                 start_frames: int = 2) -> None:
        self.pump = Pump(source)
        self.sample_rate = sample_rate
        self.vad = webrtcvad.Vad(vad_level)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_speech_frames = max(1, max_speech_ms // FRAME_MS)
        # Начинаем запись только после нескольких подряд речевых кадров:
        # одиночный щелчок или стук посуды VAD принимает за речь.
        self.start_frames = max(1, start_frames)
        # Небольшой предбуфер, чтобы не отрезать начало слова.
        self.preroll = deque(maxlen=max(1, 300 // FRAME_MS))
        self._muted = threading.Event()

    # Пока робот говорит — не слушаем: иначе он расслышит сам себя.
    def mute(self) -> None:
        self._muted.set()

    def unmute(self) -> None:
        self._muted.clear()
        self.preroll.clear()
        # Выбрасываем всё, что накопилось, пока робот говорил и думал.
        self.pump.drop_pending()

    def utterances(self) -> Iterator[bytes]:
        self.pump.start()
        speech: list[np.ndarray] = []
        silence = 0
        voiced_run = 0
        talking = False

        for frame in self.pump.frames():
            if self._muted.is_set():
                speech.clear()
                silence = voiced_run = 0
                talking = False
                continue

            is_speech = self.vad.is_speech(frame.tobytes(), self.sample_rate)

            if not talking:
                self.preroll.append(frame)
                voiced_run = voiced_run + 1 if is_speech else 0
                if voiced_run >= self.start_frames:
                    talking = True
                    speech = list(self.preroll)
                    silence = 0
                continue

            speech.append(frame)
            silence = 0 if is_speech else silence + 1

            # Либо человек замолчал, либо говорит слишком долго — режем.
            too_long = len(speech) >= self.max_speech_frames
            if silence >= self.silence_frames or too_long:
                if too_long:
                    log.info("аудио: фраза длиннее %d с, режу", self.max_speech_frames * FRAME_MS // 1000)
                talking = False
                voiced_run = 0
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
