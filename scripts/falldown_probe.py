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
    разбор.add_argument("--каждый", type=int, default=15,
                        help="печатать каждый N-й кадр, чтобы не залить экран")
    разбор.add_argument("--поза", default="",
                        help="как называется поза: стою / сижу / нагнулся / лежу")
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
    print(f"смотрю {ТОПИК} {дано.секунд:.0f} с"
          + (f", поза: {дано.поза}" if дано.поза else
             ". Держи ОДНУ позу — итог считается по всему прогону"))
    print()

    край = time.monotonic() + дано.секунд
    кадров = 0
    собрано = {"корпус": [], "ноги": [], "ось": [], "силуэт": [],
               "вытянутость": []}
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
            подробно = falldown.разобрать(точки, falldown.рамка_тела(цель))
            if подробно:
                for ключ, куда in собрано.items():
                    if подробно.get(ключ) is not None:
                        куда.append(подробно[ключ])
            if кадров % дано.каждый:
                continue
            если = "нет скелета" if подробно is None else (
                {True: "ЛЕЖИТ", False: "стоит", None: "не сужу"}[
                    подробно["лежит"]])
            номер = цель.get("track_id")
            print(f"цель {номер}: {если:9} | "
                  + (falldown.объяснить(подробно) if подробно else ""))
    связь.close()

    # Главное — вот это. Отдельные кадры шумят так, что по ним ничего не
    # настроить: у стоящего человека силуэт гулял от 0.38 до 1.52. Решение в
    # роботе принимается по медианам, значит и мерить надо медианы.
    print(f"\n=== ИТОГ{(' — поза: ' + дано.поза) if дано.поза else ''} ===")
    print(f"кадров со скелетом: {кадров}")
    for ключ, числа in собрано.items():
        if not числа:
            print(f"  {ключ:12}: замеров нет")
            continue
        числа = sorted(числа)
        def доля(ч):
            return числа[min(len(числа) - 1, int(len(числа) * ч))]
        print(f"  {ключ:12}: медиана {доля(0.5):6.2f} | "
              f"четверти {доля(0.25):6.2f}..{доля(0.75):6.2f} | "
              f"край {числа[0]:6.2f}..{числа[-1]:6.2f} | замеров {len(числа)}")
    print()
    print("пороги в роботе (по медианам): корпус <= %.0f, ось <= %.0f, "
          "силуэт >= %.2f" % (falldown.МЕДИАНА_КОРПУС, falldown.МЕДИАНА_ОСЬ,
                              falldown.МЕДИАНА_СИЛУЭТ))
    if not кадров:
        print("Скелета не было ни разу. Либо детектор не видит человека, "
              "либо он отдаёт не 19 точек — проверь journalctl -u robot-body")
    return 0


if __name__ == "__main__":
    sys.exit(main())
