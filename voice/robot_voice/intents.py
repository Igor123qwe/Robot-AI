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
    if re.fullmatch(rf"{_PREFIX}(стоп|стой|стоять|остановись|тормози|хватит|замри)", t):
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


def _timer(t: str) -> Match | None:
    m = re.fullmatch(
        rf"{_PREFIX}(?:постав\w+|заведи|засеки)?\s*таймер\s*(?:на)?\s*"
        rf"(\d+(?:[.,]\d+)?)\s*(минут\w*|мин|секунд\w*|сек|час\w*)",
        t.strip(),
    )
    if not m:
        return None

    value = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    if unit.startswith("сек"):
        minutes = value / 60
    elif unit.startswith("час"):
        minutes = value * 60
    else:
        minutes = value
    return Match("set_timer", {"minutes": round(minutes, 3)}, "таймер")


def _list_timers(t: str) -> Match | None:
    if re.fullmatch(rf"{_PREFIX}(какие\s+)?таймер\w*(\s+(идут|остались|есть))?", t):
        return Match("list_timers", {}, "список таймеров")
    return None


_RULES: list[Callable[[str], Match | None]] = [
    _stop, _list_timers, _timer, _drive, _turn, _battery,
]
