"""Распознавание речи — faster-whisper на CPU RDK X5."""

from __future__ import annotations

import io
import logging
import time

log = logging.getLogger(__name__)


class Recognizer:
    def __init__(self, model_size: str, language: str) -> None:
        from faster_whisper import WhisperModel

        log.info("whisper: загружаю модель %s", model_size)
        # int8 — единственный вменяемый вариант для восьми ядер A55.
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self.language = language
        log.info("whisper: готов")

    def transcribe(self, wav_bytes: bytes) -> str:
        started = time.monotonic()
        segments, _info = self.model.transcribe(
            io.BytesIO(wav_bytes),
            language=self.language,
            beam_size=1,             # жадный поиск: быстрее, для команд хватает
            vad_filter=False,        # тишину уже отрезал webrtcvad
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("whisper: %.1f с → %r", time.monotonic() - started, text)
        return text
