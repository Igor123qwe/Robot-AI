"""Разбор времени: «в семь утра», «через двадцать минут», «завтра в восемь».

Отдельно от intents.py, потому что это самостоятельная задача со своими
углами: часть суток, переход через полночь, «завтра», двузначные минуты
без двоеточия (Whisper пишет «7 30», а не «7:30»).

Текст сюда приходит уже после numerals_to_digits — числительные заменены
цифрами, поэтому «в семь утра» выглядит как «в 7 утра».
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_PART = {
    "утра": "am", "утром": "am",
    "дня": "pm", "вечера": "pm", "вечером": "pm",
    "ночи": "night", "ночью": "night",
}

# «в 7», «на 7 30», «к 19:45», «в 8 часов вечера».
_CLOCK = re.compile(
    r"(?:^|\s)(?:в|на|к|во)\s+(\d{1,2})(?:[ .:-]+(\d{2}))?\s*"
    r"(?:час\w*\s*)?(утра|утром|дня|вечера|вечером|ночи|ночью)?(?=\s|$)"
)

# «в час дня» — единственное время, которое говорят словом, а не числом:
# «час» для numerals_to_digits не числительное, а единица измерения.
_ONE = re.compile(r"(?:^|\s)(?:в|к|во)\s+час\s+(дня|ночи)(?=\s|$)")
_NOON = re.compile(r"(?:^|\s)(?:в|к|во)\s+(полдень|полночь)(?=\s|$)")


def at_time(text: str, now: datetime | None = None) -> datetime | None:
    """Ближайший момент, когда наступит названное время. None — времени нет.

    Без уточнения части суток берётся ближайшее наступление: «разбуди в семь»,
    сказанное вечером, — это семь утра завтра, а не семь вечера сегодня.
    """
    now = now or datetime.now()

    special = _NOON.search(text)
    if special:
        return _next(now, 12 if special.group(1) == "полдень" else 0, 0, text)
    special = _ONE.search(text)
    if special:
        return _next(now, 13 if special.group(1) == "дня" else 1, 0, text)

    m = _CLOCK.search(text)
    if not m:
        return None

    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        return None

    part = _PART.get(m.group(3) or "")
    if part == "am":
        hour = 0 if hour == 12 else hour
    elif part == "pm":
        hour = hour if hour >= 12 else hour + 12
    elif part == "night":
        # «в двенадцать ночи» — полночь, «в час ночи» — час, «в десять ночи» — 22.
        hour = 0 if hour == 12 else (hour if hour <= 5 else (hour + 12) % 24)

    return _next(now, hour, minute, text)


def _next(now: datetime, hour: int, minute: int, text: str) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if re.search(r"\bзавтра\b", text):
        target += timedelta(days=1)
    elif target <= now:
        target += timedelta(days=1)
    return target


def minutes_until(when: datetime, now: datetime | None = None) -> float:
    return round(((when or datetime.now()) - (now or datetime.now())).total_seconds() / 60, 3)


def moment(text: str, now: datetime | None = None) -> datetime | None:
    """Момент из строки: сначала как готовая дата, потом как живая фраза.

    Правила уже разобрали фразу целиком и знают точный момент — вместе с
    «завтра». Но в инструмент едет строка, и раньше там оставалось одно
    время суток: «напомни завтра в девять» превращалось в «в 9:00», а
    инструмент разбирал это заново и брал БЛИЖАЙШИЕ девять часов, то есть
    сегодняшние. Напоминание срабатывало на сутки раньше, молча.

    Поэтому правила отдают дату целиком, а модель по-прежнему говорит
    по-человечески — принимаем оба вида.
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        pass
    # Числительные словами — тоже наш случай: модель пишет «семь утра», а
    # разбор ждёт цифр. Импорт внутри функции, потому что intents сам зовёт
    # этот модуль, и наверху получилось бы кольцо.
    from .intents import normalize
    return at_time(f"в {normalize(t)}", now)
