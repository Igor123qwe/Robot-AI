#!/usr/bin/env python3
"""Сколько робот проезжает после команды «стоп». Меряем, а не гадаем.

ЗАЧЕМ. В follow.py стоит ЗАМЕДЛЕНИЕ = 0.8 м/с² с честной пометкой: «взято с
запасом вниз, настоящего никто не мерил». На этом числе держится ВСЯ
арифметика скорости — формула

    v = -aT + sqrt((aT)² + 2a·(свободно - запас))

решает, как быстро роботу разрешено ехать при данном свободном расстоянии.
Занижено замедление — робот ползает без нужды. Завышено — тормозной путь не
помещается туда, куда его посчитали, и робот доезжает до ноги.

Гадать здесь нельзя, а померить просто: разогнать до известной скорости,
скомандовать ноль и посмотреть по одометрии, сколько он ещё проехал.

    путь_после_нуля = v²/(2a)   →   a = v²/(2·путь)

БЕЗОПАСНОСТЬ. Робот РАЗГОНЯЕТСЯ И ЕДЕТ, и в этом весь смысл проверки.
Поэтому:

    НУЖНО ТРИ МЕТРА СВОБОДНОГО ПОЛА ПРЯМО ПЕРЕД РОБОТОМ и никого на пути.

Скрипт откажется работать, пока человек не подтвердит это словом, и будет
разгоняться постепенно — от медленного к быстрому, — останавливаясь сразу,
как только увидит, что места мало.

    python3 scripts/measure_braking.py

Померив, ПОПРАВЬ ЧИСЛО В follow.py — там, где оно объявлено, а не подгоняй
под него скорость.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time

import websocket

ТОПИК = "/cmd_vel"
ОДОМЕТРИЯ = "/odom"

# На каких скоростях мерим. От медленной к быстрой: если робот тормозит хуже,
# чем думали, это станет видно на первой же и до опасной мы не дойдём.
СКОРОСТИ = (0.20, 0.35, 0.50)
# Сколько разгоняться до замера. Полторы секунды хватает: разгон ограничен
# 0.5 м/с², то есть до 0.5 м/с робот выходит за секунду.
РАЗГОН = 1.5
# Сколько ждать после команды «ноль», прежде чем считать, что доехал.
ЖДЁМ_ОСТАНОВКИ = 2.0
# Насколько маленький сдвиг считаем шумом энкодеров.
ШУМ = 0.005


class Замер:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: websocket.WebSocketApp | None = None
        self._на_связи = threading.Event()
        self._замок = threading.Lock()
        self._путь = 0.0
        self._было: tuple[float, float] | None = None
        self._скорость = 0.0

    def start(self) -> None:
        threading.Thread(target=self._жить, daemon=True).start()

    def _жить(self) -> None:
        self._ws = websocket.WebSocketApp(
            self.url, on_open=self._открылось, on_message=self._сообщение)
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _открылось(self, ws) -> None:
        self._послать({"op": "advertise", "topic": ТОПИК,
                       "type": "geometry_msgs/msg/Twist"})
        self._послать({"op": "subscribe", "topic": ОДОМЕТРИЯ,
                       "type": "nav_msgs/msg/Odometry"})
        self._на_связи.set()

    def _послать(self, что: dict) -> None:
        if self._ws is not None:
            self._ws.send(json.dumps(что))

    def _сообщение(self, ws, сырое: str) -> None:
        try:
            тело = json.loads(сырое)["msg"]
            точка = тело["pose"]["pose"]["position"]
            x, y = float(точка["x"]), float(точка["y"])
        except (ValueError, KeyError, TypeError):
            return
        скорость = 0.0
        try:
            скорость = float(тело["twist"]["twist"]["linear"]["x"])
        except (KeyError, TypeError, ValueError):
            pass
        with self._замок:
            if self._было is not None:
                дx, дy = x - self._было[0], y - self._было[1]
                self._путь += math.hypot(дx, дy)
            self._было = (x, y)
            self._скорость = скорость

    @property
    def путь(self) -> float:
        with self._замок:
            return self._путь

    @property
    def скорость(self) -> float:
        with self._замок:
            return self._скорость

    def ехать(self, v: float) -> None:
        self._послать({
            "op": "publish", "topic": ТОПИК,
            "msg": {"linear": {"x": v, "y": 0.0, "z": 0.0},
                    "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}})

    def стоп(self) -> None:
        for _ in range(5):
            self.ехать(0.0)
            time.sleep(0.02)


def _служба_жива(имя: str) -> bool:
    try:
        готово = subprocess.run(["systemctl", "is-active", "--quiet", имя],
                                check=False)
    except FileNotFoundError:
        return False
    return готово.returncode == 0


def один_замер(замер: Замер, v: float) -> tuple[float, float] | None:
    """Разогнаться до v и затормозить. Возврат (скорость, путь после нуля)."""
    print(f"\n== {v:.2f} м/с")
    конец = time.monotonic() + РАЗГОН
    while time.monotonic() < конец:
        замер.ехать(v)
        time.sleep(1 / 15)
    разогнались = замер.скорость or v
    print(f"   разогнались до {разогнались:.2f} м/с по одометрии")

    # Команда «ноль» — и БОЛЬШЕ НИ ОДНОЙ КОМАНДЫ. Именно так это и выглядит в
    # жизни: сторож пути публикует ноль и перестаёт слать ход.
    до = замер.путь
    замер.стоп()
    time.sleep(ЖДЁМ_ОСТАНОВКИ)
    после = замер.путь - до
    print(f"   после нуля проехал {после:.3f} м")
    if после < ШУМ:
        print("   это шум энкодеров — тормозной путь ниже разрешения")
        return разогнались, 0.0
    return разогнались, после


def main() -> int:
    url = os.getenv("ROBOT_ROSBRIDGE", "ws://127.0.0.1:9090")
    print(__doc__.split("БЕЗОПАСНОСТЬ.")[0].strip())
    print()
    print("НУЖНО ТРИ МЕТРА СВОБОДНОГО ПОЛА ПЕРЕД РОБОТОМ И НИКОГО НА ПУТИ.")
    print()
    for служба in ("robot-deadman", "robot-voice"):
        if _служба_жива(служба):
            print(f"Сначала останови {служба}:  sudo systemctl stop {служба}")
            print("Иначе он вмешается в замер и померится он, а не тормоза.")
            return 2

    ответ = input("Впереди три метра пусто? напиши «да»: ")
    if ответ.strip().lower() not in ("да", "yes", "y"):
        print("Не подтверждено — не меряю.")
        return 1

    замер = Замер(url)
    замер.start()
    if not замер._на_связи.wait(10):
        print(f"Нет связи с rosbridge на {url}.")
        return 3

    было = замер.путь
    time.sleep(1.0)
    if замер.путь == было and замер._было is None:
        print(f"Шасси молчит в {ОДОМЕТРИЯ} — мерить нечем.")
        return 3

    замедления: list[float] = []
    try:
        for v in СКОРОСТИ:
            итог = один_замер(замер, v)
            if итог is None:
                continue
            скорость, путь = итог
            if путь <= ШУМ:
                continue
            a = скорость * скорость / (2 * путь)
            замедления.append(a)
            print(f"   замедление {a:.2f} м/с²")
            time.sleep(1.0)
    finally:
        замер.стоп()

    print()
    if not замедления:
        print("Тормозной путь везде ниже разрешения одометрии.")
        print("Это значит, что робот тормозит быстрее, чем можно померить, —")
        print("то есть нынешние 0.8 м/с² с запасом. Оставь как есть.")
        return 0

    # Берём ХУДШЕЕ, а не среднее. Ошибиться здесь можно только в одну
    # сторону: завышенное замедление разрешает скорость, на которой робот
    # не успеет остановиться.
    худшее = min(замедления)
    print(f"Замедления по замерам: "
          f"{', '.join(f'{a:.2f}' for a in замедления)} м/с²")
    print(f"ХУДШЕЕ: {худшее:.2f} м/с². Его и надо ставить — не среднее:")
    print("ошибиться здесь можно только в одну сторону.")
    print()
    print("Поправь в voice/robot_voice/follow.py:")
    print(f"    ЗАМЕДЛЕНИЕ = {худшее:.2f}")
    if худшее < 0.8:
        print()
        print("Внимание: это МЕНЬШЕ нынешних 0.8 — значит робот тормозит хуже,")
        print("чем считает код, и разрешённая скорость сейчас завышена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
