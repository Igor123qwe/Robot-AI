#!/usr/bin/env python3
"""Что шасси вообще отдаёт: список топиков ROS и что в них лежит.

    python3 scripts/ros_topics.py              # всё, что есть
    python3 scripts/ros_topics.py колёс        # только про колёса
    python3 scripts/ros_topics.py --слушать /odom

Зачем отдельный скрипт, когда есть ros2 topic list. Затем, что наш голосовой
сервис живёт ВНЕ окружения ROS: у него свой venv, без rclpy и без source
setup.bash, и разговаривает он с шасси через rosbridge. Спрашивать «а что там
есть» надо оттуда же, откуда мы потом будем это читать, — иначе легко найти
топик, который прекрасно виден из ROS и недоступен нам.

Сейчас он нужен ради одного конкретного вопроса: отдаёт ли шасси скорости
ЧЕТЫРЁХ КОЛЁС ПО ОТДЕЛЬНОСТИ. Если да, у нас появляется бесплатный измеритель
проскальзывания — у меканума четыре колеса дают четыре уравнения на три
неизвестных, и невязка лишнего уравнения равна нулю ровно тогда, когда ролики
не скользят. А от того, скользят они или нет, зависит, можно ли вообще строить
карту на этой одометрии.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

СЛОВА_ПРО_КОЛЁСА = ("wheel", "joint", "encoder", "motor", "speed", "vel",
                    "odom", "imu")


def связаться(адрес: str):
    try:
        import websocket
    except ImportError:
        print("Нет websocket-client. В окружении робота он есть:",
              file=sys.stderr)
        print("    ~/Robot-AI/voice/.venv/bin/python scripts/ros_topics.py",
              file=sys.stderr)
        raise SystemExit(1)
    try:
        return websocket.create_connection(адрес, timeout=10)
    except Exception as e:                    # noqa: BLE001
        print(f"Не подключился к {адрес}: {e}", file=sys.stderr)
        print("Живёт ли мост:  systemctl status robot-bridge", file=sys.stderr)
        raise SystemExit(1)


def спросить(ws, служба: str, аргументы: dict | None = None,
             ждать: float = 10.0) -> dict:
    """Вызвать службу rosapi и дождаться ответа именно на неё."""
    метка = f"вопрос-{служба}"
    ws.send(json.dumps({"op": "call_service", "service": служба,
                        "args": аргументы or {}, "id": метка}))
    срок = time.monotonic() + ждать
    while time.monotonic() < срок:
        try:
            ответ = json.loads(ws.recv())
        except Exception:                     # noqa: BLE001
            break
        # Сверяем метку: в сокет может прилететь что угодно ещё, и брать
        # первое пришедшее значит однажды принять чужой ответ за свой.
        if ответ.get("id") == метка:
            return ответ.get("values") or {}
    return {}


def слушать(ws, топик: str, секунд: float) -> None:
    """Показать, что в топике на самом деле лежит."""
    ws.send(json.dumps({"op": "subscribe", "topic": топик, "id": "слушаю"}))
    print(f"\nслушаю {топик} {секунд:.0f} с…\n")
    срок = time.monotonic() + секунд
    сколько = 0
    while time.monotonic() < срок:
        try:
            ответ = json.loads(ws.recv())
        except Exception:                     # noqa: BLE001
            break
        if ответ.get("topic") != топик:
            continue
        сколько += 1
        if сколько <= 3 or сколько % 20 == 0:
            print(f"  {сколько}: {json.dumps(ответ.get('msg'), ensure_ascii=False)[:400]}")
    print(f"\nвсего сообщений: {сколько}")
    if not сколько:
        print("Тишина. Либо топика нет, либо в него никто не пишет.")


def главное() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("искать", nargs="?", default="",
                   help="показать только топики, где встречается это слово")
    р.add_argument("--адрес", default="ws://127.0.0.1:9090")
    р.add_argument("--слушать", default="", metavar="ТОПИК")
    р.add_argument("--секунд", type=float, default=3.0)
    аргс = р.parse_args()

    ws = связаться(аргс.адрес)
    try:
        if аргс.слушать:
            слушать(ws, аргс.слушать, аргс.секунд)
            return 0

        данные = спросить(ws, "/rosapi/topics")
        топики = данные.get("topics") or []
        типы = данные.get("types") or []
        if not топики:
            print("Мост ответил, а топиков не назвал. Живо ли шасси:")
            print("    systemctl status robot-base")
            return 1

        пары = sorted(zip(топики, типы + [""] * len(топики)))
        if аргс.искать:
            нужное = аргс.искать.lower()
            пары = [(т, тип) for т, тип in пары if нужное in т.lower()]

        print(f"топиков: {len(пары)}\n")
        for топик, тип in пары:
            print(f"  {топик:44} {тип}")

        # Отдельно и заметно — то, ради чего скрипт и написан.
        колёсные = [т for т, _ in пары
                    if any(с in т.lower() for с in СЛОВА_ПРО_КОЛЁСА)]
        print("\nПохоже на данные о движении:")
        for т in колёсные:
            print(f"  {т}")
        if not колёсные:
            print("  ничего. Значит скорости колёс по отдельности шасси не "
                  "отдаёт,\n  и проскальзывание измерить нечем.")
        else:
            print("\nПосмотреть, что внутри:")
            print(f"    python3 scripts/ros_topics.py --слушать {колёсные[0]}")
    finally:
        ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(главное())
