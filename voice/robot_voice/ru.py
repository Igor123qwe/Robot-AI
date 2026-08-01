"""Числа, время и даты словами.

Всё, что говорит робот, уходит в Piper, а тот читает написанное буквально.
«14:35» он произносит как «четырнадцать двоеточие тридцать пять», «1.5 мин» —
как «один точка пять мин». Поэтому числа в репликах пишутся словами.

Модель это делает сама (так сказано в системном промпте), но правила и
инструменты отвечают в обход модели — им нужен такой же перевод.
"""

from __future__ import annotations

from datetime import datetime

_UNITS = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь",
          "восемь", "девять", "десять", "одиннадцать", "двенадцать",
          "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать")
_TENS = {2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят",
         6: "шестьдесят", 7: "семьдесят", 8: "восемьдесят", 9: "девяносто"}
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот")
# Женский род нужен минутам и секундам: «одна минута», «двадцать две секунды».
_FEMALE = {"один": "одна", "два": "две"}
# Винительный падеж — для «поставь таймер на одну минуту».
_FEMALE_ACC = {"один": "одну", "два": "две"}

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря")
_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница",
             "суббота", "воскресенье")
_ORDINALS = ("", "первое", "второе", "третье", "четвёртое", "пятое", "шестое",
             "седьмое", "восьмое", "девятое", "десятое", "одиннадцатое",
             "двенадцатое", "тринадцатое", "четырнадцатое", "пятнадцатое",
             "шестнадцатое", "семнадцатое", "восемнадцатое", "девятнадцатое",
             "двадцатое")


def cardinal(n: int, *, female: bool = False, accusative: bool = False) -> str:
    """Число словами, 0–999. Больше роботу не нужно: дальше идут метры и минуты."""
    n = int(n)
    if n < 0 or n > 999:
        return str(n)

    words = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if rest or not hundreds:
        if rest < 20:
            words.append(_UNITS[rest])
        else:
            tens, unit = divmod(rest, 10)
            words.append(_TENS[tens])
            if unit:
                words.append(_UNITS[unit])

    if female:
        forms = _FEMALE_ACC if accusative else _FEMALE
        words[-1] = forms.get(words[-1], words[-1])
    return " ".join(words)


def plural(n: float, one: str, few: str, many: str) -> str:
    """Согласование существительного с числом: 1 минута, 2 минуты, 5 минут."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def count(n: int, one: str, few: str, many: str, *,
          female: bool = False, accusative: bool = False) -> str:
    """Число с существительным: «пять минут», «двадцать два градуса»."""
    return (f"{cardinal(n, female=female, accusative=accusative)} "
            f"{plural(n, one, few, many)}")


def volts(v: float) -> str:
    """«двенадцать и четыре вольта» — десятичную точку вслух не читают."""
    whole = int(v)
    tenth = int(round((v - whole) * 10))
    if tenth == 10:
        whole, tenth = whole + 1, 0
    if not tenth:
        return count(whole, "вольт", "вольта", "вольт")
    return f"{cardinal(whole)} и {cardinal(tenth)} вольта"


def duration(seconds: float, *, accusative: bool = False) -> str:
    """Длительность словами: «девять минут», «час двадцать минут».

    accusative — для «поставил таймер на одну минуту».
    """
    total = max(0, int(round(seconds)))
    sec_forms = ("секунду" if accusative else "секунда", "секунды", "секунд")
    min_forms = ("минуту" if accusative else "минута", "минуты", "минут")
    if total < 60:
        return count(total, *sec_forms, female=True, accusative=accusative)

    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(count(hours, "час", "часа", "часов"))
    if minutes:
        parts.append(count(minutes, *min_forms, female=True, accusative=accusative))
    # Секунды при часах — лишняя точность для разговора.
    if sec and not hours:
        parts.append(count(sec, *sec_forms, female=True, accusative=accusative))
    return " ".join(parts)


def clock(when: datetime | None = None) -> str:
    """Время словами: «четырнадцать часов тридцать пять минут»."""
    when = when or datetime.now()
    if not when.minute:
        return f"{_bare_clock(when)} ровно"
    return _bare_clock(when)


def _bare_clock(when: datetime) -> str:
    """То же без «ровно»: «в семь часов ровно» звучит по-канцелярски."""
    hours = count(when.hour, "час", "часа", "часов")
    if not when.minute:
        return hours
    return f"{hours} {count(when.minute, 'минута', 'минуты', 'минут', female=True)}"


def _ordinal(day: int) -> str:
    if day <= 20:
        return _ORDINALS[day]
    if day == 30:
        return "тридцатое"
    tens, unit = divmod(day, 10)
    return f"{_TENS[tens]} {_ORDINALS[unit]}"


def when_phrase(target: datetime, now: datetime | None = None) -> str:
    """«сегодня в семь часов», «завтра в семь часов тридцать минут»."""
    now = now or datetime.now()
    days = (target.date() - now.date()).days
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(days)
    if day is None:
        day = f"{_ordinal(target.day)} {_MONTHS[target.month - 1]}"
    return f"{day} в {_bare_clock(target)}"


def date(when: datetime | None = None) -> str:
    """Дата словами: «пятница, первое августа»."""
    when = when or datetime.now()
    return (f"{_WEEKDAYS[when.weekday()]}, "
            f"{_ordinal(when.day)} {_MONTHS[when.month - 1]}")
