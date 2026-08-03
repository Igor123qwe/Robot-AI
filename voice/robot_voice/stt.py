"""Распознавание речи.

Двумя способами. Основной — на домашнем ПК: там видеокарта, и та же фраза
разбирается за доли секунды и самой крупной моделью. Запасной — здесь же, на
роботе: медленно и самой лёгкой моделью, зато без чужой помощи.

Разница не косметическая. На Cortex-A55 без видеокарты Whisper тратит впятеро
больше времени, чем длится сама фраза, и вынужденно работает моделью, которая
половину слов перевирает. Каждая такая ошибка — это ещё один круг «не
расслышал» и, если фраза дошла до модели, оплаченный запрос впустую.
"""

from __future__ import annotations

import io
import logging
import os
import time

log = logging.getLogger(__name__)

# Дозвон до ПК — коротко: он или отвечает сразу, или его нет. Само
# распознавание может занять секунды, поэтому чтение ждём дольше.
PC_CONNECT = 2.0
PC_READ = 30.0

# Столько не трогаем ПК после отказа. Иначе каждая фраза будет начинаться с
# ожидания связи, и выключенный компьютер станет хуже, чем его отсутствие.
PC_DOWN = 60.0

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
        # Насколько модель уверена в последнем услышанном. Отрицательное:
        # около -0.2 — уверенно, ниже -1 — почти наверняка выдумала. Держим
        # отдельным полем, а не в возвращаемом значении: слушатель один, а
        # менять форму ответа ради этого пришлось бы во всех вызовах.
        self.confidence: float | None = None
        log.info("whisper: готов")

    def transcribe(self, wav_bytes: bytes) -> str:
        started = time.monotonic()
        segments, info = self._run(io.BytesIO(wav_bytes))

        parts, scores = [], []
        for s in segments:
            # Сегмент, который сама модель считает тишиной, — это выдумка.
            if getattr(s, "no_speech_prob", 0.0) > 0.85:
                log.debug("whisper: отбросил сегмент как тишину (%r)", s.text.strip())
                continue
            parts.append(s.text.strip())
            score = getattr(s, "avg_logprob", None)
            if score is not None:
                scores.append(float(score))

        self.confidence = sum(scores) / len(scores) if scores else None
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


class Remote:
    """Распознаёт на домашнем ПК. Не отвечает — переходим на свои силы.

    Местная модель поднимается лениво, при первом же отказе ПК. Грузить её
    заранее — это полторы сотни мегабайт памяти и несколько секунд старта за
    то, чем в обычный день не пользуются ни разу.
    """

    def __init__(self, url: str, make_local) -> None:
        self.url = url
        self._make_local = make_local
        self._local = None
        self._down_until = 0.0
        self._said_local = False
        # Уверенность последнего распознавания — см. Recognizer.confidence.
        self.confidence: float | None = None

    def transcribe(self, wav_bytes: bytes) -> str:
        if time.monotonic() >= self._down_until:
            text = self._ask_pc(wav_bytes)
            if text is not None:
                return text
        return self._fallback(wav_bytes)

    def _ask_pc(self, wav_bytes: bytes) -> str | None:
        import requests

        started = time.monotonic()
        try:
            resp = requests.post(
                self.url, data=wav_bytes,
                headers={"Content-Type": "audio/wav"},
                timeout=(PC_CONNECT, PC_READ),
            )
            resp.raise_for_status()
            body = resp.json()
            text = (body.get("text") or "").strip()
            self.confidence = body.get("sure")
        except Exception as e:
            self._down_until = time.monotonic() + PC_DOWN
            log.warning("ПК не распознал (%s) — перехожу на свои силы", e)
            return None
        log.info("ПК: %.2f с | уверенность %s → %r",
                 time.monotonic() - started,
                 f"{self.confidence:.2f}" if self.confidence is not None else "—",
                 text)
        self._said_local = False
        return text

    def _fallback(self, wav_bytes: bytes) -> str:
        if self._local is None:
            log.info("поднимаю распознавание на роботе — это займёт время")
            self._local = self._make_local()
        if not self._said_local:
            self._said_local = True
            log.warning("распознаю сам: медленнее и хуже, чем на ПК")
        text = self._local.transcribe(wav_bytes)
        self.confidence = self._local.confidence
        return text
