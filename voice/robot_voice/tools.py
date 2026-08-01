"""Инструменты, которые модель может вызывать: движение, батарея, таймеры.

Схемы описаны руками, а не выведены из сигнатур. Причина простая: робот ходит
не только к api.anthropic.com, но и через сторонний роутер, а тот понимает
обычный /v1/messages и не обязан поддерживать beta-эндпоинт с его
автогенерацией. Явная схема работает везде одинаково.

Описания в схемах пишутся для модели, а не для человека — по ним она решает,
что и когда вызывать.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import ru

log = logging.getLogger(__name__)

# Как зовётся таймер, которому названия не дали.
NO_NAME = "без названия"


def _ring(label: str) -> str:
    name = "Таймер" if label == NO_NAME else f"Таймер {label}"
    return f"{name} — время вышло."

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


def _say_distance(metres: float) -> str:
    """Расстояние словами: «полметра» звучит естественнее, чем «0.50 м»."""
    if abs(metres - 0.5) < 0.02:
        return "полметра"
    if abs(metres - 1.0) < 0.02:
        return "метр"
    if abs(metres - 1.5) < 0.02:
        return "полтора метра"
    if metres < 1.0:
        cm = int(round(metres * 100))
        return "на " + ru.count(cm, "сантиметр", "сантиметра", "сантиметров")
    whole = int(metres)
    half = round(metres - whole, 2)
    if half >= 0.45:
        return f"на {ru.cardinal(whole)} с половиной метра"
    return "на " + ru.count(whole, "метр", "метра", "метров")


def _say_percent(pct: float) -> str:
    return ru.count(int(round(pct)), "процент", "процента", "процентов")


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
    """Таймеры, которые робот озвучивает вслух, когда они срабатывают.

    Хранятся на диске: автообновление перезапускает сервис каждые две минуты,
    если в репозитории что-то поменялось, и таймер на духовку не должен от
    этого пропадать. Сроки записываем в обычном времени (time.time), а не в
    monotonic — оно не переживает перезапуск.
    """

    def __init__(self, announce: Callable[[str], None],
                 store: Path | None = None) -> None:
        self._announce = announce
        self._items: dict[str, tuple[threading.Timer, float]] = {}
        # Снятые с паузы: имя → сколько секунд оставалось.
        self._paused: dict[str, float] = {}
        self._lock = threading.Lock()
        self.store = store

    def add(self, label: str, seconds: float) -> None:
        with self._lock:
            self.cancel(label, _locked=True)
            timer = threading.Timer(seconds, self._fire, args=(label,))
            timer.daemon = True
            self._items[label] = (timer, time.monotonic() + seconds)
            timer.start()
            self._save()

    def cancel(self, label: str, *, _locked: bool = False) -> bool:
        if not _locked:
            self._lock.acquire()
        try:
            paused = self._paused.pop(label, None)
            item = self._items.pop(label, None)
            if item is not None:
                item[0].cancel()
            if item is not None or paused is not None:
                self._save()
            return item is not None or paused is not None
        finally:
            if not _locked:
                self._lock.release()

    def pause(self, label: str) -> bool:
        """Останавливает отсчёт, запомнив остаток."""
        with self._lock:
            item = self._items.pop(label, None)
            if item is None:
                return False
            timer, due = item
            timer.cancel()
            self._paused[label] = max(0.0, due - time.monotonic())
            self._save()
            return True

    def resume(self, label: str) -> bool:
        with self._lock:
            left = self._paused.pop(label, None)
        if left is None:
            return False
        self.add(label, left)   # add берёт тот же замок, поэтому уже снаружи
        return True

    def remaining(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            return {label: max(0.0, due - now) for label, (_, due) in self._items.items()}

    def paused(self) -> dict[str, float]:
        with self._lock:
            return dict(self._paused)

    def cancel_all(self) -> None:
        with self._lock:
            for timer, _ in self._items.values():
                timer.cancel()
            self._items.clear()
            self._paused.clear()
            self._save()

    def _fire(self, label: str) -> None:
        with self._lock:
            self._items.pop(label, None)
            self._save()
        self._announce(_ring(label))

    # --- переживание перезапуска ----------------------------------------
    def _save(self) -> None:
        """Пишет состояние на диск. Замок уже держит вызывающий."""
        if self.store is None:
            return
        shift = time.time() - time.monotonic()
        data = {
            "items": {label: due + shift for label, (_, due) in self._items.items()},
            "paused": dict(self._paused),
        }
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_name(self.store.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(self.store)      # замена целиком, без полуфайлов
        except OSError as e:
            log.warning("не смог сохранить таймеры: %s", e)

    def restore(self) -> None:
        """Поднимает таймеры, пережившие перезапуск сервиса."""
        if self.store is None or not self.store.exists():
            return
        try:
            data = json.loads(self.store.read_text())
        except (OSError, ValueError) as e:
            log.warning("не смог прочитать сохранённые таймеры: %s", e)
            return

        now = time.time()
        late = []
        for label, due in (data.get("items") or {}).items():
            left = due - now
            if left > 1:
                self.add(label, left)
                log.info("восстановил таймер %r, осталось %.0f с", label, left)
            else:
                late.append(label)
        with self._lock:
            self._paused.update(data.get("paused") or {})
            self._save()
        for label in late:
            # Сработал, пока сервиса не было. Молчать нельзя: человек ждал.
            self._announce(f"Пока меня не было, {_ring(label).lower()}")


def build_tools(ros, timers: Timers) -> list[Tool]:
    """Собирает набор инструментов, привязанный к конкретному роботу."""

    def battery_guard() -> str | None:
        # Без связи с шасси команда просто утонет: rosbridge молча выбрасывает
        # публикацию. Раньше робот в этом случае бодро отвечал «Еду вперёд» и
        # не ехал — час подряд, пока не заметишь.
        if not getattr(ros, "connected", True):
            return "Не могу: нет связи с шасси. Проверь, включён ли робот."
        volt = ros.voltage
        if volt is None:
            return None  # телеметрия ещё не пришла — не блокируем
        if volt < CUTOFF_VOLT:
            return (f"Не поеду: батарея {ru.volts(volt)}, "
                    f"это {_say_percent(volt_to_percent(volt))}. Нужна зарядка.")
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
        # Не блокируем: пока едем, робот должен слышать «стоп».
        ros.drive(fx * DRIVE_SPEED, fy * DRIVE_SPEED, 0.0, duration)
        return f"Еду {key} {_say_distance(distance)}."

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
        turn_words = ru.count(int(round(degrees)), "градус", "градуса", "градусов")
        return f"Разворачиваюсь {key} на {turn_words}."

    def stop() -> str:
        was_moving = ros.moving
        ros.stop_motion()
        log.info("стоп")
        return "Остановился." if was_moving else "Я и так стою."

    def battery() -> str:
        volt = ros.voltage
        if volt is None:
            return "Телеметрия батареи пока не пришла — шасси не отвечает."
        note = ""
        if volt < CUTOFF_VOLT:
            note = " Ехать уже нельзя, нужна зарядка."
        elif volt < 10.8:
            note = " Пора на зарядку."
        return (f"Батарея {ru.volts(volt)}, это примерно "
                f"{_say_percent(volt_to_percent(volt))}.{note}")

    def _named(label: str) -> str:
        """«таймер лапша» или просто «таймер», если названия нет."""
        return "таймер" if label == NO_NAME else f"таймер {label}"

    def _the_only(label: str, pool: dict[str, float]) -> str | None:
        """Имя единственного таймера — когда названное не нашлось.

        Таймер часто зовут не так, как он записан: «сними таймер на пять минут»
        вместо названия. Если он всего один, гадать не о чем.
        """
        if label in pool:
            return label
        return next(iter(pool)) if len(pool) == 1 else None

    def set_timer(minutes: float, label: str = NO_NAME) -> str:
        minutes = max(0.1, min(600.0, float(minutes)))
        label = label.strip() or NO_NAME
        extra = False
        if label == NO_NAME:
            # Второй безымянный таймер раньше молча затирал первый: имя одно,
            # а add() заменяет по имени. Теперь второй зовётся по времени.
            taken = {**timers.remaining(), **timers.paused()}
            if label in taken:
                label = ru.duration(minutes * 60)
                while label in taken:
                    label += " ещё"
                extra = True
        timers.add(label, minutes * 60)
        log.info("таймер %r на %.1f мин", label, minutes)
        how_long = ru.duration(minutes * 60, accusative=True)
        if extra:
            return f"Поставил ещё один таймер, на {how_long}."
        return f"Поставил {_named(label)} на {how_long}."

    def list_timers() -> str:
        left = timers.remaining()
        held = timers.paused()
        if not left and not held:
            return "Активных таймеров нет."
        parts = [f"{_named(label)} — осталось {ru.duration(sec)}"
                 for label, sec in left.items()]
        parts += [f"{_named(label)} на паузе, на нём {ru.duration(sec)}"
                  for label, sec in held.items()]
        listed = "; ".join(parts)
        return listed[0].upper() + listed[1:]

    def _which(label: str, pool: dict[str, float], question: str) -> str:
        """Переспрос, когда непонятно, о каком именно таймере речь."""
        names = ["безымянный" if n == NO_NAME else n for n in pool]
        listed = names[0] if len(names) == 1 else \
            ", ".join(names[:-1]) + " и " + names[-1]
        head = f"Таймера {label} нет. " if label else ""
        return f"{head}Есть {listed}. {question}"

    def cancel_timer(label: str = "") -> str:
        label = label.strip()
        pool = {**timers.remaining(), **timers.paused()}
        if not pool:
            return "Активных таймеров нет."
        target = _the_only(label, pool)
        if target is None:
            return _which(label, pool, "Какой снять?")
        timers.cancel(target)
        return f"Отменил {_named(target)}."

    def pause_timer(label: str = "") -> str:
        label = label.strip()
        running = timers.remaining()
        if not running:
            return "Идущих таймеров нет."
        target = _the_only(label, running)
        if target is None:
            return _which(label, running, "Какой поставить на паузу?")
        timers.pause(target)
        return f"Остановил {_named(target)}, на нём {ru.duration(running[target])}."

    def resume_timer(label: str = "") -> str:
        label = label.strip()
        held = timers.paused()
        if not held:
            return "На паузе ничего нет."
        target = _the_only(label, held)
        if target is None:
            return _which(label, held, "Какой продолжить?")
        timers.resume(target)
        return f"Продолжаю {_named(target)}, осталось {ru.duration(held[target])}."

    def cancel_all_timers() -> str:
        count = len(timers.remaining()) + len(timers.paused())
        if not count:
            return "Активных таймеров и так нет."
        timers.cancel_all()
        word = ru.plural(count, "он был", "их было", "их было")
        return f"Отменил все таймеры, {word} {ru.cardinal(count)}."

    def time_now() -> str:
        return f"Сейчас {ru.clock()}."

    def date_now() -> str:
        return f"Сегодня {ru.date()}."

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
            name="time_now",
            description="Узнать текущее время. Часов у тебя нет, поэтому "
                        "спрашивай здесь, а не отвечай по памяти.",
            input_schema=EMPTY_SCHEMA,
            run=time_now,
        ),
        Tool(
            name="date_now",
            description="Узнать сегодняшнюю дату и день недели. Календаря у "
                        "тебя нет, поэтому спрашивай здесь.",
            input_schema=EMPTY_SCHEMA,
            run=date_now,
        ),
        Tool(
            name="list_timers",
            description="Показать, какие таймеры сейчас идут и сколько им осталось.",
            input_schema=EMPTY_SCHEMA,
            run=list_timers,
        ),
        Tool(
            name="cancel_timer",
            description="Снять таймер. Без названия — если таймер всего один.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, если их несколько.",
                    },
                },
            },
            run=cancel_timer,
        ),
        Tool(
            name="pause_timer",
            description="Приостановить таймер, не сбрасывая его. Без названия — "
                        "если таймер всего один.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, если их несколько.",
                    },
                },
            },
            run=pause_timer,
        ),
        Tool(
            name="resume_timer",
            description="Продолжить таймер, снятый с паузы.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, если на паузе их несколько.",
                    },
                },
            },
            run=resume_timer,
        ),
        Tool(
            name="cancel_all_timers",
            description="Отменить сразу все идущие таймеры.",
            input_schema=EMPTY_SCHEMA,
            run=cancel_all_timers,
        ),
    ]
