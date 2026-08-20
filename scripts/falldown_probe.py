#!/usr/bin/env python3
"""Что сторож падений видит прямо сейчас: живые числа по каждому человеку.

    ~/Robot-AI/voice/.venv/bin/python scripts/falldown_probe.py

Зачем. Робот трижды подряд объявил упавшим человека, который спокойно стоял, и
починить это по журналу было нельзя: в журнале была одна строка «лежит дольше
2.5 с» и ни одного числа. Пороги в таком положении можно только угадывать, а
угадывать пороги на живом роботе — это вечер на каждую попытку.

Скрипт печатает по строке на кадр: оба признака, их числа и итог. Встань,
сядь, нагнись, ляг — и станет видно, какие числа у КАЖДОЙ позы на ЭТОЙ камере,
которая стоит в двадцати пяти сантиметрах от пола и смотрит снизу вверх.
Числа из статей и чужих пакетов сняты с камеры на высоте человека и к нашей
геометрии отношения не имеют.

Читаем через rosbridge, а не через rclpy, по той же причине, что и
scripts/ros_topics.py: голосовой сервис живёт вне окружения ROS, и смотреть на
данные надо оттуда же, откуда их потом будет читать робот.

Порт дальномера скрипт не трогает и колёсам ничего не шлёт — только слушает.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "voice"))

from robot_voice import falldown            # noqa: E402

ТОПИК = "/hobot_mono2d_body_detection"


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__)
    разбор.add_argument("--адрес", default="ws://127.0.0.1:9090")
    разбор.add_argument("--секунд", type=float, default=60.0,
                        help="сколько смотреть")
    разбор.add_argument("--каждый", type=int, default=5,
                        help="печатать каждый N-й кадр, чтобы не залить экран")
    дано = разбор.parse_args()

    try:
        import websocket
    except ImportError:
        print("Нет websocket-client. Запускай венвом робота:", file=sys.stderr)
        print("    ~/Robot-AI/voice/.venv/bin/python "
              "scripts/falldown_probe.py", file=sys.stderr)
        return 1

    связь = websocket.create_connection(дано.адрес, timeout=5)
    связь.send(json.dumps({"op": "subscribe", "topic": ТОПИК,
                           "type": "ai_msgs/msg/PerceptionTargets",
                           "throttle_rate": 100}))
    print(f"смотрю {ТОПИК} {дано.секунд:.0f} с. Встань, сядь, нагнись, ляг.")
    print("порог лежания: корпус <= %.0f°, ноги <= %.0f°, разбег <= %.0f°, "
          "силуэт >= %.2f, вытянутость >= %.1f"
          % (falldown.КОРПУС_НИЗКО, falldown.НОГИ_ВЫСОКО, falldown.РАЗБЕГ,
             falldown.ЛЕЖИТ_ШИРЕ, falldown.ВЫТЯНУТ_ОТ))
    print()

    край = time.monotonic() + дано.секунд
    кадров = 0
    связь.settimeout(1.0)
    while time.monotonic() < край:
        try:
            сырьё = связь.recv()
        except Exception:                       # noqa: BLE001 — просто тишина
            continue
        try:
            письмо = json.loads(сырьё)
        except ValueError:
            continue
        if письмо.get("topic") != ТОПИК:
            continue
        цели = (письмо.get("msg") or {}).get("targets") or []
        for цель in цели:
            точки = falldown.скелет(цель)
            if точки is None:
                continue
            кадров += 1
            if кадров % дано.каждый:
                continue
            подробно = falldown.разобрать(точки, falldown.рамка_тела(цель))
            если = "нет скелета" if подробно is None else (
                {True: "ЛЕЖИТ", False: "стоит", None: "не сужу"}[
                    подробно["лежит"]])
            номер = цель.get("track_id")
            print(f"цель {номер}: {если:9} | "
                  + (falldown.объяснить(подробно) if подробно else ""))
    связь.close()
    print(f"\nвсего кадров со скелетом: {кадров}")
    if not кадров:
        print("Скелета не было ни разу. Либо детектор не видит человека, "
              "либо он отдаёт не 19 точек — проверь journalctl -u robot-body")
    return 0


if __name__ == "__main__":
    sys.exit(main())
