"""Распознавание речи — faster-whisper на CPU RDK X5."""

from __future__ import annotations

import io
import logging
import os
import time

log = logging.getLogger(__name__)

# Подсказки модели (initial_prompt) здесь НЕТ, и это осознанно. Казалось бы,
# список доменных слов должен помогать с короткими командами — на деле на
# шумной или тихой записи Whisper начинает просто повторять слова из подсказки:
# «сантиметров, сантиметров, сантиметров…». Заодно это разгоняет генерацию до
# предела и запускает повторный проход по порогу повторов, так что секунда
# звука обрабатывается полминуты. Проверено на живом роботе — не возвращать.

# Сколько ядер отдать распознаванию. Одно оставляем системе: на нём крутятся
# узел шасси, rosbridge и веб-сервер, и подвешивать их ради Whisper незачем.
def _threads() -> int:
    return max(1, min(6, (os.cpu_count() or 2) - 1))


class Recognizer:
    def __init__(self, model_size: str, language: str, *, beam_size: int = 1) -> None:
        from faster_whisper import WhisperModel

        threads = _threads()
        log.info("whisper: загружаю модель %s на %d ядрах", model_size, threads)
        # int8 — единственный вменяемый вариант для восьми ядер A55.
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8",
                                  cpu_threads=threads)
        self.language = language
        self.beam_size = beam_size
        self._warned_slow = False
        log.info("whisper: готов")

    def transcribe(self, wav_bytes: bytes) -> str:
        started = time.monotonic()
        segments, info = self._run(io.BytesIO(wav_bytes))

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
        ratio = spent / audio_len if audio_len else 0.0
        log.info("whisper: %.1f с на %.1f с звука (×%.1f) → %r",
                 spent, audio_len, ratio, text)

        if ratio > 3 and not self._warned_slow:
            self._warned_slow = True
            log.warning("whisper медленнее звука втрое — возьмите модель полегче: "
                        "ROBOT_WHISPER_MODEL=tiny")
        return text

    def _run(self, stream):
        """Запуск с защитой от зацикливания.

        Часть параметров появилась в свежих версиях faster-whisper. Если
        установленная их не знает — отступаем к базовому набору, а не падаем.
        """
        common = dict(
            language=self.language,
            beam_size=self.beam_size,     # 1 — жадный поиск, для команд хватает
            vad_filter=False,             # тишину уже отрезал webrtcvad
            condition_on_previous_text=False,   # иначе модель зацикливается
            # Отсечки против выдумок на шуме и тишине.
            temperature=[0.0, 0.2],
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        extra = dict(
            repetition_penalty=1.15,
            no_repeat_ngram_size=3,
        )
        try:
            return self.model.transcribe(stream, **common, **extra)
        except TypeError:
            log.info("whisper: версия без защиты от повторов, работаю без неё")
            stream.seek(0)
            return self.model.transcribe(stream, **common)
