"""Распознавание речи — faster-whisper на CPU RDK X5."""

from __future__ import annotations

import io
import logging
import time

log = logging.getLogger(__name__)

# Подсказка модели о том, какая лексика ожидается. Whisper использует её как
# начало контекста, и это заметно помогает с короткими командами: без неё
# «вперёд» на шумной записи превращается то в «вперде», то в «в перед».
INITIAL_PROMPT = (
    "Робот, вперёд, назад, влево, вправо, стоп, стой, "
    "развернись, разворот, градусов, метр, сантиметров, "
    "таймер, минут, секунд, батарея, заряд, процентов."
)


class Recognizer:
    def __init__(self, model_size: str, language: str, *, beam_size: int = 1) -> None:
        from faster_whisper import WhisperModel

        log.info("whisper: загружаю модель %s", model_size)
        # int8 — единственный вменяемый вариант для восьми ядер A55.
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self.language = language
        self.beam_size = beam_size
        log.info("whisper: готов")

    def transcribe(self, wav_bytes: bytes) -> str:
        started = time.monotonic()
        segments, info = self.model.transcribe(
            io.BytesIO(wav_bytes),
            language=self.language,
            beam_size=self.beam_size,     # 1 — жадный поиск, для команд хватает
            initial_prompt=INITIAL_PROMPT,
            vad_filter=False,             # тишину уже отрезал webrtcvad
            condition_on_previous_text=False,   # иначе модель зацикливается
            # Отсечки против выдумок на шуме и тишине. Значения whisper'овские
            # по умолчанию, но заданы явно: молча полагаться на них не стоит,
            # они меняются от версии к версии.
            temperature=[0.0, 0.2, 0.4],
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )

        parts = []
        for s in segments:
            # Сегмент, который сама модель считает тишиной, — это выдумка.
            if getattr(s, "no_speech_prob", 0.0) > 0.85:
                log.debug("whisper: отбросил сегмент как тишину (%r)", s.text.strip())
                continue
            parts.append(s.text.strip())

        text = " ".join(p for p in parts if p).strip()
        spent = time.monotonic() - started
        audio_len = getattr(info, "duration", 0.0) or 0.0
        log.info("whisper: %.1f с на %.1f с звука (×%.1f) → %r",
                 spent, audio_len, (spent / audio_len if audio_len else 0), text)
        return text
