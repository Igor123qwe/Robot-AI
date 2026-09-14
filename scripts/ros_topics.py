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


# Поля, которые почти всегда мусор для человека и всегда длинные. Детектор
# людей кладёт в perfs замеры времени каждого этапа — десяток объектов на
# каждое сообщение, — и они выдавливают из обрезки ровно то, ради чего
# смотрят: targets. Один раз уже выдавили.
ШУМНЫЕ = ("perfs", "covariance", "orientation_covariance",
          "angular_velocity_covariance", "linear_acceleration_covariance")


def _почистить(узел):
    """Убрать заведомо шумные поля, оставив всё остальное как есть."""
    if isinstance(узел, dict):
        return {к: _почистить(в) for к, в in узел.items() if к not in ШУМНЫЕ}
    if isinstance(узел, list):
        return [_почистить(э) for э in узел]
    return узел


def _достать(узел, путь: str):
    """Значение по пути вида targets.0.rois — или None, если пути нет.

    Пустые шаги пропускаем, и это не педантизм. Лишняя точка на конце —
    «targets.» — раскалывала путь на «targets» и пустую строку, которая не
    находилась никогда. Поле объявлялось всегда пустым, и пробник уверенно
    сообщал «детектор работает, а видеть ему некого» — в тот момент, когда
    человек сидел прямо перед камерой. Ошибка, которая не падает и не молчит,
    а врёт правдоподобно.
    """
    for шаг in [ш for ш in путь.split(".") if ш]:
        if isinstance(узел, list):
            try:
                узел = узел[int(шаг)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(узел, dict) or шаг not in узел:
            return None
        узел = узел[шаг]
    return узел


def слушать(ws, топик: str, секунд: float, поле: str = "",
            знаков: int = 1200) -> None:
    """Показать, что в топике на самом деле лежит."""
    ws.send(json.dumps({"op": "subscribe", "topic": топик, "id": "слушаю"}))
    print(f"\nслушаю {топик} {секунд:.0f} с…\n")
    срок = time.monotonic() + секунд
    сколько = 0
    с_добычей = 0
    while time.monotonic() < срок:
        try:
            ответ = json.loads(ws.recv())
        except Exception:                     # noqa: BLE001
            break
        if ответ.get("topic") != топик:
            continue
        сколько += 1
        тело = ответ.get("msg")
        if поле:
            # Показываем только сообщения, где искомое поле НЕ пустое:
            # детектор шлёт тридцать кадров в секунду, и в большинстве из них
            # человека нет. Печатать их все — значит утопить те три, ради
            # которых смотрят.
            добыча = _достать(тело, поле)
            if not добыча:
                continue
            с_добычей += 1
            if с_добычей <= 5:
                print(f"  {сколько}: {поле} = "
                      f"{json.dumps(добыча, ensure_ascii=False)[:знаков]}")
            continue
        if сколько <= 3 or сколько % 20 == 0:
            текст = json.dumps(_почистить(тело), ensure_ascii=False)
            print(f"  {сколько}: {текст[:знаков]}")
    print(f"\nвсего сообщений: {сколько}")
    if поле:
        print(f"из них с непустым «{поле}»: {с_добычей}")
        if not с_добычей and сколько:
            print("Сообщения идут, но поле всюду пустое. Либо детектору "
                  "правда некого видеть, либо поле зовётся иначе — проверь "
                  "без --поле, что в сообщении вообще есть.")
    if not сколько:
        print("Тишина. Либо топика нет, либо в него никто не пишет.")


ИНТЕРЕСНОЕ = (
    "/joint_states", "/odom", "/odom_combined", "/imu/data_raw",
    "/ultrasonic_data_A", "/ultrasonic_data_B", "/ultrasonic_data_C",
    "/ultrasonic_data_D", "/ultrasonic_data_E", "/ultrasonic_data_F",
    "/RangerAvoidFlag", "/robot_charging_flag", "/robot_recharge_flag",
    "/chassis_security", "/tf_static",
)


def разведать(ws, секунд: float) -> None:
    """Подписаться на всё интересное разом и показать по образцу из каждого.

    Одной командой вместо пятнадцати. Ради этого и написано: выяснять по
    одному топику за заход — это пятнадцать раз сходить к роботу и обратно,
    и на третьем разе человек бросает.
    """
    for топик in ИНТЕРЕСНОЕ:
        ws.send(json.dumps({"op": "subscribe", "topic": топик,
                            "id": "разведка", "throttle_rate": 200}))
    print("\nслушаю %d топиков %.0f с…\n" % (len(ИНТЕРЕСНОЕ), секунд))

    образцы: dict[str, dict] = {}
    сколько: dict[str, int] = {}
    срок = time.monotonic() + секунд
    while time.monotonic() < срок:
        try:
            ответ = json.loads(ws.recv())
        except Exception:                     # noqa: BLE001
            break
        топик = ответ.get("topic")
        if not топик or "msg" not in ответ:
            continue
        сколько[топик] = сколько.get(топик, 0) + 1
        образцы.setdefault(топик, ответ["msg"])

    for топик in ИНТЕРЕСНОЕ:
        if топик not in образцы:
            print(f"  {топик}: молчит")
            continue
        сообщений = сколько.get(топик, 0)
        герц = сообщений / max(секунд, 0.001)
        текст = json.dumps(образцы[топик], ensure_ascii=False)
        print(f"  {топик}  ({герц:.0f} Гц)")
        print(f"    {текст[:600]}")
    молчат = [т for т in ИНТЕРЕСНОЕ if т not in образцы]
    if молчат:
        print("\nМолчащие топики существуют, но в них никто не пишет. Для "
              "\n«/robot_charging_*» это норма: они оживают на станции.")


def главное() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("искать", nargs="?", default="",
                   help="показать только топики, где встречается это слово")
    р.add_argument("--адрес", default="ws://127.0.0.1:9090")
    р.add_argument("--слушать", default="", metavar="ТОПИК")
    р.add_argument("--секунд", type=float, default=3.0)
    р.add_argument("--поле", default="", metavar="ПУТЬ",
                   help="показать только это поле, например targets. "
                        "Печатаются лишь сообщения, где оно не пустое")
    р.add_argument("--знаков", type=int, default=1200,
                   help="сколько знаков показывать (по умолчанию 1200)")
    р.add_argument("--разведать", action="store_true",
                   help="послушать всё интересное разом и показать образцы")
    аргс = р.parse_args()

    ws = связаться(аргс.адрес)
    try:
        if аргс.разведать:
            разведать(ws, max(аргс.секунд, 4.0))
            return 0
        if аргс.слушать:
            слушать(ws, аргс.слушать, аргс.секунд, аргс.поле, аргс.знаков)
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
