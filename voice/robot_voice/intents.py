"""Быстрый разбор простых команд без обращения к модели.

Смысл в том, что «стоп» или «вперёд на метр» — это не задача для языковой
модели. Такие фразы разбираются регулярками мгновенно, бесплатно и предсказуемо,
а модель нужна там, где начинается разговор.

Правила намеренно строгие: совпало — выполняем, не совпало — отдаём модели.
Ошибиться в сторону «не понял и спросил умного» дешевле, чем поехать не туда.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# Слова-приставки, которыми люди начинают команду. Их можно просто отбросить.
_PREFIX = r"(?:а\s+)?(?:ну\s+)?(?:давай\s+)?(?:пожалуйста\s+)?"
_VERB_MOVE = r"(?:про)?(?:едь|езжай|поезжай|двигайся|иди|двинься|сдвинься)?"
_VERB_TURN = r"(?:по|раз)?(?:вернись|верни|ворачивайся)?"

_DIR_WORDS = {
    "вперед": "вперёд", "вперёд": "вперёд", "прямо": "вперёд",
    "назад": "назад", "обратно": "назад",
    "влево": "влево", "налево": "влево", "лево": "влево",
    "вправо": "вправо", "направо": "вправо", "право": "вправо",
}

# Расстояния словами — то, что реально говорят вслух.
_WORD_DISTANCE = {
    "полметра": 0.5, "пол метра": 0.5, "метр": 1.0, "метра": 1.0,
    "два метра": 2.0, "три метра": 3.0, "чуть": 0.2, "чуть-чуть": 0.2,
    "немного": 0.3, "немножко": 0.3,
}

_WORD_ANGLE = {
    "кругом": 180.0, "на пол оборота": 180.0, "полоборота": 180.0,
    "четверть": 90.0, "чуть": 30.0, "чуть-чуть": 30.0, "немного": 30.0,
}


def normalize(text: str) -> str:
    """Приводит фразу к виду, удобному для регулярок."""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s.,-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,-")
    return t


def _to_metres(value: str, unit: str) -> float:
    v = float(value.replace(",", "."))
    if unit.startswith("см") or unit.startswith("сантиметр"):
        return v / 100
    return v


@dataclass
class Match:
    """Что распознало правило: имя инструмента и аргументы к нему."""

    tool: str
    args: dict
    rule: str


def parse(text: str) -> Match | None:
    """Разбирает фразу. None — значит это разговор, отдаём модели."""
    t = normalize(text)
    if not t or len(t) > 60:      # длинная фраза почти наверняка не команда
        return None

    for rule in _RULES:
        m = rule(t)
        if m is not None:
            log.info("правило %s: %s(%s)", m.rule, m.tool, m.args)
            return m
    return None


# --- сами правила ----------------------------------------------------------
def _stop(t: str) -> Match | None:
    # Слова остановки собраны из русских интентов Home Assistant плюс то,
    # что говорят живому роботу. «Забудь», «неважно», «проехали» сюда
    # намеренно не попали: это отмена просьбы, а не команда встать.
    if re.fullmatch(
        rf"{_PREFIX}(стоп|стоп[- ]стоп|стой|стоять|стой на месте|остановись|"
        rf"[при]?останови(сь)?|тормози|тормозни|притормози|хватит|прекрати|"
        rf"замри|отставить|отбой)",
        t,
    ):
        return Match("stop", {}, "стоп")
    return None


def _battery(t: str) -> Match | None:
    if re.search(r"(заряд|батаре|аккумулятор|сколько.*процент)", t) and len(t.split()) <= 6:
        return Match("battery", {}, "батарея")
    return None


def _drive(t: str) -> Match | None:
    dirs = "|".join(_DIR_WORDS)
    # «проедь вперёд на 1.5 метра», «вперёд», «назад чуть-чуть»
    m = re.fullmatch(
        rf"{_PREFIX}{_VERB_MOVE}\s*({dirs})"
        rf"(?:\s+на)?\s*"
        rf"(?:(\d+(?:[.,]\d+)?)\s*(м|метр\w*|см|сантиметр\w*)|([а-я -]+))?",
        t.strip(),
    )
    if not m:
        return None

    direction = _DIR_WORDS[m.group(1)]
    args: dict = {"direction": direction}

    if m.group(2):
        args["distance"] = _to_metres(m.group(2), m.group(3) or "м")
    elif m.group(4):
        word = m.group(4).strip()
        if word not in _WORD_DISTANCE:
            return None       # непонятный хвост — пусть разбирается модель
        args["distance"] = _WORD_DISTANCE[word]

    return Match("drive", args, "движение")


def _turn(t: str) -> Match | None:
    if re.fullmatch(rf"{_PREFIX}кругом", t):
        return Match("turn", {"direction": "влево", "degrees": 180.0}, "разворот")

    m = re.fullmatch(
        rf"{_PREFIX}(?:по|раз)(?:вернись|верни|ворот)\w*\s*"
        rf"(налево|направо|влево|вправо|лево|право)?"
        rf"(?:\s+на)?\s*"
        rf"(?:(\d+)\s*(?:градус\w*)?|([а-я -]+))?",
        t.strip(),
    )
    if not m:
        return None

    direction = _DIR_WORDS.get(m.group(1) or "", "влево")
    if direction not in ("влево", "вправо"):
        return None

    args: dict = {"direction": direction}
    if m.group(2):
        args["degrees"] = float(m.group(2))
    elif m.group(3):
        word = m.group(3).strip()
        if word not in _WORD_ANGLE:
            return None
        args["degrees"] = _WORD_ANGLE[word]

    return Match("turn", args, "разворот")


# Глаголы постановки таймера — из русских интентов Home Assistant.
_TIMER_SET = r"(?:установи\w*|постав(?:ь|ить)|включи\w*|запусти\w*|зада(?:й|ть)|заведи|засеки)"
_TIMER_CANCEL = r"(?:отмени\w*|удали\w*|сбрось|сбрось\w*|убери|выключи\w*|останови\w*|сними)"
_UNITS = r"(минут\w*|мин|секунд\w*|сек|час\w*|ч)"


def _unit_to_minutes(value: float, unit: str) -> float:
    if unit.startswith(("сек", "сек")):
        return value / 60
    if unit.startswith(("час", "ч")):
        return value * 60
    return value


def _timer(t: str) -> Match | None:
    # «поставь таймер лапша на 9 минут», «таймер на 1 час 30 минут»
    m = re.fullmatch(
        rf"{_PREFIX}{_TIMER_SET}?\s*таймер\s*"
        rf"(?:([а-я][а-я -]{{0,20}}?)\s+)?"          # необязательное название
        rf"(?:на\s*)?"
        rf"(\d+(?:[.,]\d+)?)\s*{_UNITS}"
        rf"(?:\s*(\d+(?:[.,]\d+)?)\s*{_UNITS})?",
        t.strip(),
    )
    if not m:
        return None

    minutes = _unit_to_minutes(float(m.group(2).replace(",", ".")), m.group(3))
    if m.group(4):
        minutes += _unit_to_minutes(float(m.group(4).replace(",", ".")), m.group(5))

    args: dict = {"minutes": round(minutes, 3)}
    label = (m.group(1) or "").strip()
    # «на» и «через» — предлоги, а не названия таймера.
    if label and label not in ("на", "через"):
        args["label"] = label
    return Match("set_timer", args, "таймер")


def _list_timers(t: str) -> Match | None:
    # Формулировки статуса взяты из HassTimerStatus.
    if re.fullmatch(
        rf"{_PREFIX}(?:"
        rf"(какие\s+)?таймер\w*(\s+(идут|остались|есть|активны))?|"
        rf"что\s+с\s+таймер\w+|"
        rf"(какое\s+)?состояние\s+(у\s+)?таймер\w+|"
        rf"сколько\s+(времени\s+)?осталось\s+(у|на)\s+таймер\w+"
        rf")",
        t,
    ):
        return Match("list_timers", {}, "список таймеров")
    return None


def _cancel_timers(t: str) -> Match | None:
    if re.fullmatch(rf"{_PREFIX}{_TIMER_CANCEL}\s+все\s+таймер\w+", t):
        return Match("cancel_all_timers", {}, "снять все таймеры")

    m = re.fullmatch(
        rf"{_PREFIX}{_TIMER_CANCEL}\s+таймер\s*([а-я][а-я -]{{0,20}})?",
        t.strip(),
    )
    if not m:
        return None
    label = (m.group(1) or "").strip()
    # Без названия отменять нечего конкретного — снимаем все, так понятнее.
    if not label:
        return Match("cancel_all_timers", {}, "снять все таймеры")
    return Match("cancel_timer", {"label": label}, "снять таймер")


_RULES: list[Callable[[str], Match | None]] = [
    _stop, _list_timers, _cancel_timers, _timer, _drive, _turn, _battery,
]
