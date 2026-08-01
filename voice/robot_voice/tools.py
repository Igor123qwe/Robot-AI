"""Инструменты, которые модель может вызывать: движение, батарея, таймеры.

Схемы описаны руками, а не выведены из сигнатур. Причина простая: робот ходит
не только к api.anthropic.com, но и через сторонний роутер, а тот понимает
обычный /v1/messages и не обязан поддерживать beta-эндпоинт с его
автогенерацией. Явная схема работает везде одинаково.

Описания в схемах пишутся для модели, а не для человека — по ним она решает,
что и когда вызывать.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

# Скорости для голосовых команд — заведомо ниже предела пульта.
DRIVE_SPEED = 0.20   # м/с
TURN_SPEED = 1.00    # рад/с

MAX_DISTANCE = 3.0   # м за одну команду
MAX_ANGLE = 360.0    # градусов

# Ниже этого напряжения не едем вообще: 3S li-ion уже на грани.
CUTOFF_VOLT = 10.2

# Разрядная кривая 3S li-ion — та же, что в веб-пульте.
_CURVE = [
    (12.60, 100), (12.30, 90), (12.00, 75), (11.70, 60),
    (11.40, 50), (11.10, 45), (10.80, 25), (10.20, 8), (9.90, 0),
]

_DIRECTIONS = {
    "вперёд": (1.0, 0.0), "вперед": (1.0, 0.0), "forward": (1.0, 0.0),
    "назад": (-1.0, 0.0), "back": (-1.0, 0.0),
    "влево": (0.0, 1.0), "left": (0.0, 1.0),
    "вправо": (0.0, -1.0), "right": (0.0, -1.0),
}

EMPTY_SCHEMA = {"type": "object", "properties": {}}


@dataclass
class Tool:
    """Инструмент: описание для модели плюс то, что реально выполняется."""

    name: str
    description: str
    input_schema: dict
    run: Callable[..., str]

    def spec(self) -> dict:
        """То, что уходит в API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __call__(self, args: dict[str, Any]) -> str:
        try:
            return self.run(**args)
        except TypeError as e:
            # Модель придумала лишний или пропустила обязательный аргумент.
            log.warning("неверные аргументы для %s: %s", self.name, e)
            return f"Не понял, как вызвать {self.name}: {e}"
        except Exception as e:
            log.exception("инструмент %s упал", self.name)
            return f"Инструмент {self.name} не сработал: {e}"


def volt_to_percent(v: float) -> float:
    if v >= _CURVE[0][0]:
        return 100.0
    if v <= _CURVE[-1][0]:
        return 0.0
    for (v_hi, p_hi), (v_lo, p_lo) in zip(_CURVE, _CURVE[1:]):
        if v_lo <= v <= v_hi:
            k = (v - v_lo) / (v_hi - v_lo)
            return p_lo + k * (p_hi - p_lo)
    return 0.0


class Timers:
    """Таймеры, которые робот озвучивает вслух, когда они срабатывают."""

    def __init__(self, announce: Callable[[str], None]) -> None:
        self._announce = announce
        self._items: dict[str, tuple[threading.Timer, float]] = {}
        self._lock = threading.Lock()

    def add(self, label: str, seconds: float) -> None:
        with self._lock:
            self.cancel(label, _locked=True)
            timer = threading.Timer(seconds, self._fire, args=(label,))
            timer.daemon = True
            self._items[label] = (timer, time.monotonic() + seconds)
            timer.start()

    def cancel(self, label: str, *, _locked: bool = False) -> bool:
        if not _locked:
            self._lock.acquire()
        try:
            item = self._items.pop(label, None)
            if item is None:
                return False
            item[0].cancel()
            return True
        finally:
            if not _locked:
                self._lock.release()

    def remaining(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            return {label: max(0.0, due - now) for label, (_, due) in self._items.items()}

    def cancel_all(self) -> None:
        with self._lock:
            for timer, _ in self._items.values():
                timer.cancel()
            self._items.clear()

    def _fire(self, label: str) -> None:
        with self._lock:
            self._items.pop(label, None)
        self._announce(f"Таймер {label} — время вышло.")


def build_tools(ros, timers: Timers) -> list[Tool]:
    """Собирает набор инструментов, привязанный к конкретному роботу."""

    def battery_guard() -> str | None:
        volt = ros.voltage
        if volt is None:
            return None  # телеметрия ещё не пришла — не блокируем
        if volt < CUTOFF_VOLT:
            return (f"Отказ: батарея {volt:.1f} В "
                    f"({volt_to_percent(volt):.0f} %), ехать нельзя, нужна зарядка.")
        return None

    def drive(direction: str, distance: float = 0.5) -> str:
        key = direction.strip().lower()
        if key not in _DIRECTIONS:
            return f"Не понял направление {direction!r}. Можно: вперёд, назад, влево, вправо."

        blocked = battery_guard()
        if blocked:
            return blocked

        distance = max(0.05, min(MAX_DISTANCE, float(distance)))
        fx, fy = _DIRECTIONS[key]
        duration = distance / DRIVE_SPEED
        log.info("еду %s на %.2f м (%.1f с)", key, distance, duration)
        ros.drive(fx * DRIVE_SPEED, fy * DRIVE_SPEED, 0.0, duration)
        return f"Проехал {key} примерно {distance:.2f} м."

    def turn(direction: str, degrees: float = 90.0) -> str:
        key = direction.strip().lower()
        if key in ("влево", "left"):
            sign = 1.0
        elif key in ("вправо", "right"):
            sign = -1.0
        else:
            return f"Не понял сторону {direction!r}. Можно: влево или вправо."

        blocked = battery_guard()
        if blocked:
            return blocked

        degrees = max(5.0, min(MAX_ANGLE, abs(float(degrees))))
        duration = (degrees * 3.14159265 / 180.0) / TURN_SPEED
        log.info("разворачиваюсь %s на %.0f° (%.1f с)", key, degrees, duration)
        ros.drive(0.0, 0.0, sign * TURN_SPEED, duration)
        return f"Развернулся {key} примерно на {degrees:.0f} градусов."

    def stop() -> str:
        ros.stop_motion()
        log.info("стоп")
        return "Остановился."

    def battery() -> str:
        volt = ros.voltage
        if volt is None:
            return "Телеметрия батареи пока не пришла — шасси не отвечает."
        pct = volt_to_percent(volt)
        note = ""
        if volt < CUTOFF_VOLT:
            note = " Ехать уже нельзя, нужна зарядка."
        elif volt < 10.8:
            note = " Пора на зарядку."
        return f"Батарея {volt:.1f} В, это примерно {pct:.0f} процентов.{note}"

    def set_timer(minutes: float, label: str = "без названия") -> str:
        minutes = max(0.1, min(600.0, float(minutes)))
        label = label.strip() or "без названия"
        timers.add(label, minutes * 60)
        log.info("таймер %r на %.1f мин", label, minutes)
        return f"Таймер {label} поставлен на {minutes:g} минут."

    def list_timers() -> str:
        left = timers.remaining()
        if not left:
            return "Активных таймеров нет."
        return "; ".join(f"{label} — осталось {sec / 60:.1f} мин"
                         for label, sec in left.items())

    def cancel_timer(label: str) -> str:
        return (f"Таймер {label} отменён." if timers.cancel(label.strip())
                else f"Таймера {label} нет.")

    def cancel_all_timers() -> str:
        count = len(timers.remaining())
        if not count:
            return "Активных таймеров и так нет."
        timers.cancel_all()
        return f"Отменил все таймеры, их было {count}."

    return [
        Tool(
            name="drive",
            description="Проехать в заданную сторону. Колёса меканум, поэтому "
                        "робот может двигаться боком, не разворачиваясь.",
            input_schema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["вперёд", "назад", "влево", "вправо"],
                        "description": "Куда ехать.",
                    },
                    "distance": {
                        "type": "number",
                        "description": "Сколько метров проехать, от 0.05 до 3.0.",
                    },
                },
                "required": ["direction"],
            },
            run=drive,
        ),
        Tool(
            name="turn",
            description="Развернуться на месте.",
            input_schema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["влево", "вправо"],
                        "description": "Влево — против часовой, вправо — по часовой.",
                    },
                    "degrees": {
                        "type": "number",
                        "description": "На сколько градусов повернуться, от 5 до 360.",
                    },
                },
                "required": ["direction"],
            },
            run=turn,
        ),
        Tool(
            name="stop",
            description="Немедленно остановить движение.",
            input_schema=EMPTY_SCHEMA,
            run=stop,
        ),
        Tool(
            name="battery",
            description="Узнать текущий заряд батареи.",
            input_schema=EMPTY_SCHEMA,
            run=battery,
        ),
        Tool(
            name="set_timer",
            description="Поставить таймер. Когда он сработает, робот скажет об этом вслух.",
            input_schema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "Через сколько минут сработает, от 0.1 до 600.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Короткое название, чтобы отличать таймеры друг от друга.",
                    },
                },
                "required": ["minutes"],
            },
            run=set_timer,
        ),
        Tool(
            name="list_timers",
            description="Показать, какие таймеры сейчас идут и сколько им осталось.",
            input_schema=EMPTY_SCHEMA,
            run=list_timers,
        ),
        Tool(
            name="cancel_timer",
            description="Отменить таймер по названию.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, который надо снять.",
                    },
                },
                "required": ["label"],
            },
            run=cancel_timer,
        ),
        Tool(
            name="cancel_all_timers",
            description="Отменить сразу все идущие таймеры.",
            input_schema=EMPTY_SCHEMA,
            run=cancel_all_timers,
        ),
    ]
