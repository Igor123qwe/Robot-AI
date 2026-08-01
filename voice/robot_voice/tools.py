"""Инструменты, которые Claude может вызывать: движение, батарея, таймеры.

Схемы генерируются из сигнатур и docstring'ов декоратором @beta_tool, поэтому
описания аргументов здесь — не комментарии для человека, а то, что видит модель.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from anthropic import beta_tool

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


def build_tools(ros, timers: Timers) -> list:
    """Собирает набор инструментов, привязанный к конкретному роботу."""

    def _battery_guard() -> str | None:
        volt = ros.voltage
        if volt is None:
            return None  # телеметрия ещё не пришла — не блокируем
        if volt < CUTOFF_VOLT:
            return (f"Отказ: батарея {volt:.1f} В "
                    f"({volt_to_percent(volt):.0f} %), ехать нельзя, нужна зарядка.")
        return None

    @beta_tool
    def drive(direction: str, distance: float = 0.5) -> str:
        """Проехать в заданную сторону. Колёса меканум, поэтому робот может
        двигаться боком, не разворачиваясь.

        Args:
            direction: Куда ехать: вперёд, назад, влево или вправо.
            distance: Сколько метров проехать, от 0.05 до 3.0.
        """
        key = direction.strip().lower()
        if key not in _DIRECTIONS:
            return f"Не понял направление {direction!r}. Можно: вперёд, назад, влево, вправо."

        blocked = _battery_guard()
        if blocked:
            return blocked

        distance = max(0.05, min(MAX_DISTANCE, float(distance)))
        fx, fy = _DIRECTIONS[key]
        duration = distance / DRIVE_SPEED
        log.info("еду %s на %.2f м (%.1f с)", key, distance, duration)
        ros.drive(fx * DRIVE_SPEED, fy * DRIVE_SPEED, 0.0, duration)
        return f"Проехал {key} примерно {distance:.2f} м."

    @beta_tool
    def turn(direction: str, degrees: float = 90.0) -> str:
        """Развернуться на месте.

        Args:
            direction: Влево (против часовой) или вправо (по часовой).
            degrees: На сколько градусов повернуться, от 5 до 360.
        """
        key = direction.strip().lower()
        if key in ("влево", "left"):
            sign = 1.0
        elif key in ("вправо", "right"):
            sign = -1.0
        else:
            return f"Не понял сторону {direction!r}. Можно: влево или вправо."

        blocked = _battery_guard()
        if blocked:
            return blocked

        degrees = max(5.0, min(MAX_ANGLE, abs(float(degrees))))
        duration = (degrees * 3.14159265 / 180.0) / TURN_SPEED
        log.info("разворачиваюсь %s на %.0f° (%.1f с)", key, degrees, duration)
        ros.drive(0.0, 0.0, sign * TURN_SPEED, duration)
        return f"Развернулся {key} примерно на {degrees:.0f} градусов."

    @beta_tool
    def stop() -> str:
        """Немедленно остановить движение."""
        ros.stop_motion()
        log.info("стоп")
        return "Остановился."

    @beta_tool
    def battery() -> str:
        """Узнать текущий заряд батареи."""
        volt = ros.voltage
        if volt is None:
            return "Телеметрия батареи пока не пришла — шасси пока не отвечает."
        pct = volt_to_percent(volt)
        note = ""
        if volt < CUTOFF_VOLT:
            note = " Ехать уже нельзя, нужна зарядка."
        elif volt < 10.8:
            note = " Пора на зарядку."
        return f"Батарея {volt:.1f} В, это примерно {pct:.0f} процентов.{note}"

    @beta_tool
    def set_timer(minutes: float, label: str = "без названия") -> str:
        """Поставить таймер. Когда он сработает, робот скажет об этом вслух.

        Args:
            minutes: Через сколько минут сработает, от 0.1 до 600.
            label: Короткое название, чтобы отличать таймеры друг от друга.
        """
        minutes = max(0.1, min(600.0, float(minutes)))
        timers.add(label.strip() or "без названия", minutes * 60)
        log.info("таймер %r на %.1f мин", label, minutes)
        return f"Таймер {label} поставлен на {minutes:g} минут."

    @beta_tool
    def list_timers() -> str:
        """Показать, какие таймеры сейчас идут и сколько им осталось."""
        left = timers.remaining()
        if not left:
            return "Активных таймеров нет."
        parts = [f"{label} — осталось {sec / 60:.1f} мин" for label, sec in left.items()]
        return "; ".join(parts)

    @beta_tool
    def cancel_timer(label: str) -> str:
        """Отменить таймер по названию.

        Args:
            label: Название таймера, который надо снять.
        """
        return (f"Таймер {label} отменён." if timers.cancel(label.strip())
                else f"Таймера {label} нет.")

    return [drive, turn, stop, battery, set_timer, list_timers, cancel_timer]
