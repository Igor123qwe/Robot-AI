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


# Заученные выдумки. Whisper учили на субтитрах с ютуба, и на тишине или шуме
# он выдаёт оттуда самые частые концовки роликов. На живом роботе за один
# вечер пришли «Спасибо за внимание!», «С вами был Игорь Негода» и
# «Продолжение следует…» — робот отвечал на них вслух, разговаривая с
# холодильником. Уверенность при этом бывает приличная (-0.73), так что
# барьером это не ловится: модель не сомневается, она вспоминает.
_MADE_UP = (
    "спасибо за внимание", "с вами был", "субтитры", "продолжение следует",
    "редактор субтитров", "корректор", "все права защищены",
    "подписывайтесь на канал", "ставьте лайки", "до новых встреч",
    "спасибо за просмотр", "перевод и озвучание", "фонд кино",
)


def made_up(text: str) -> bool:
    """Похоже ли услышанное на заученную концовку ролика, а не на речь."""
    bare = text.lower().replace("ё", "е").strip(" .,!?…-«»\"")
    return any(bare.startswith(p) or bare == p for p in _MADE_UP)


# Столько знаков человек в одну фразу не говорит. Запись обрывается по тишине
# раньше, чем наберётся столько осмысленного текста.
ПРЕДЕЛ_ФРАЗЫ = 300


def зациклилось(text: str) -> bool:
    """Не сорвалась ли модель в повтор одного и того же.

    Это не выдумка из субтитров, а другая беда: на шуме Whisper иногда уходит
    в петлю и печатает одно слово или букву до упора. На живом роботе пришло
    «Ууууу…» на двести двадцать знаков — и, что важно, с ВЫСОКОЙ уверенностью
    (−0.07). Барьер по уверенности такое пропускает: модель не сомневается,
    она зациклилась. Дальше этот мусор ушёл в языковую модель, та выдала
    восемь тысяч токенов, и робот молчал полминуты, теряя звук.

    Ловим по двум приметам, обе дешёвые. Длинная череда одной буквы — такого
    в русском не бывает даже в «ммм» и «ааа». И слишком много знаков на слово:
    у настоящей речи слова короткие, у петли одно слово растягивается на всю
    строку.
    """
    чистый = (text or "").strip()
    if not чистый:
        return False
    if len(чистый) > ПРЕДЕЛ_ФРАЗЫ:
        return True
    буквы = [б for б in чистый.lower() if б.isalpha()]
    подряд = длина = 1
    for первая, вторая in zip(буквы, буквы[1:]):
        длина = длина + 1 if первая == вторая else 1
        подряд = max(подряд, длина)
    # Пять одинаковых букв кряду — уже не речь; но «ааа» из трёх оставляем.
    if подряд >= 5:
        return True
    слова = чистый.split()
    # Одно слово длиннее сорока знаков — это склеенная петля, а не слово.
    return bool(слова) and max(len(с) for с in слова) > 40


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
        # Кого узнал ПК по голосу и насколько похоже. Своего узнавания у
        # робота нет: модель для этого нужна отдельная, а видеокарты у него
        # нет вовсе.
        self.speaker = ""
        self.similarity = 0.0
        self.tag = ""
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
        if made_up(text):
            log.info("whisper: выдумал концовку ролика (%r) — считаю тишиной", text)
            text, self.confidence = "", None
        elif зациклилось(text):
            log.info("whisper: зациклился (%d знаков) — считаю тишиной", len(text))
            text, self.confidence = "", None
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
        # Кого узнал ПК по голосу и насколько похоже.
        self.speaker = ""
        self.similarity = 0.0
        self.tag = ""

    @property
    def сам_разбираю(self) -> bool:
        """Разбираю ли я речь сам, потому что ПК не отвечает.

        Наружу это нужно, чтобы сказать человеку. Молча уйти на своё
        распознавание — значит оставить его гадать, почему робот вдруг стал
        медленным и глухим: в журнале-то всё написано, а вслух ничего.

        Свойство обязано жить ЗДЕСЬ, а не в Recognizer. Раньше оно стояло там,
        и не работало никогда — ни разу за всю жизнь робота. У Recognizer нет
        поля _down_until: он вообще не знает про ПК, он и есть «свои силы».
        Чтение свойства кидало AttributeError, а сторож читает его через
        getattr с умолчанием — и умолчание это исключение молча проглатывало.
        У Remote же, который про ПК знает всё, свойства не было вовсе, и
        getattr так же молча возвращал False.

        Итого признак «робот оглох, потому что ПК выключен» не загорался ни в
        одной из двух возможных сборок, а самопроверка этого не видела: в ней
        стояла заглушка, у которой признак есть.
        """
        return time.monotonic() < self._down_until

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
            # Кто это сказал. Пусто — ПК не узнал голос или узнавание
            # выключено; робот тогда разговаривает, никого не различая.
            self.speaker = (body.get("кто") or "").strip()
            self.similarity = float(body.get("похожесть") or 0.0)
            # Метка слепка этой фразы. По ней робот потом скажет ПК «это
            # говорили мне» — и только тогда голос попадёт в память.
            self.tag = (body.get("метка") or "").strip()
        except Exception as e:
            self._down_until = time.monotonic() + PC_DOWN
            log.warning("ПК не распознал (%s) — перехожу на свои силы", e)
            return None
        # Фильтр выдумок нужен и здесь: на ПК своя модель, но выдумывает она
        # то же самое и по той же причине — оба Whisper учили на субтитрах.
        if made_up(text):
            log.info("ПК выдумал концовку ролика (%r) — считаю тишиной", text)
            text, self.confidence = "", None
        elif зациклилось(text):
            # Уверенность тут высокая, поэтому обычный барьер не сработает:
            # модель не сомневается, она зациклилась.
            log.info("ПК зациклился (%d знаков) — считаю тишиной", len(text))
            text, self.confidence = "", None
        log.info("ПК: %.2f с | уверенность %s | %s → %r",
                 time.monotonic() - started,
                 f"{self.confidence:.2f}" if self.confidence is not None else "—",
                 f"{self.speaker} {self.similarity:.2f}" if self.speaker
                 else f"не узнал {self.similarity:.2f}",
                 text)
        self._said_local = False
        return text

    def прогреть(self) -> None:
        """Поднять своё распознавание заранее, в фоне.

        Раньше модель поднималась в тот самый миг, когда ПК замолчал, — то
        есть когда человек уже ждёт ответа. На роботе это не секунды, и со
        стороны выглядит как «замолчал и думает». Память под неё тратится
        всегда, даже когда ПК жив весь день, — это осознанный размен: полторы
        сотни мегабайт против минуты немоты в единственный важный момент.
        Кому память дороже — ROBOT_WARM_LOCAL_STT=0.
        """
        if self._local is not None:
            return
        try:
            self._local = self._make_local()
            log.info("своё распознавание прогрето и ждёт на случай, "
                     "если ПК замолчит")
        except Exception as e:                  # noqa: BLE001
            log.warning("не смог прогреть своё распознавание (%s) — "
                        "подниму, когда понадобится", e)

    def _fallback(self, wav_bytes: bytes) -> str:
        # Узнавание по голосу живёт на ПК. Раз мы сюда попали, ПК недоступен —
        # и тащить в разговор имя от прошлой фразы нельзя: робот показал бы
        # одному человеку записи про другого.
        self.speaker, self.similarity, self.tag = "", 0.0, ""
        if self._local is None:
            log.info("поднимаю распознавание на роботе — это займёт время")
            self._local = self._make_local()
        if not self._said_local:
            self._said_local = True
            log.warning("распознаю сам: медленнее и хуже, чем на ПК")
        text = self._local.transcribe(wav_bytes)
        self.confidence = self._local.confidence
        return text
