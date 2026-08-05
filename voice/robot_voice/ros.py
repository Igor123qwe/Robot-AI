"""Связь с ROS 2 через rosbridge (websocket, порт 9090).

Прямой rclpy тут не используется намеренно: голосовой пайплайн живёт в своём
venv с faster-whisper, а окружение ROS тянуть в него не хочется. rosbridge уже
поднят для веб-пульта, так что переиспользуем его.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time

import websocket

log = logging.getLogger(__name__)

CMD_VEL = "/cmd_vel"
POWER = "/PowerVoltage"
# Куда шасси докладывает, где оно на самом деле. Без этого поездка отмеряется
# секундомером: «полметра» — это «две с половиной секунды на такой-то
# скорости». Разгон при этом никто не считает, и робот проезжает меньше, а
# разворот «на сто восемьдесят» выходит на глаз градусов на сто двадцать.
ODOM = "/odom"
# Дальше этого одометрию считаем протухшей: шасси молчит или узел упал.
ODOM_TTL = 2.0
# Сколько ждать, пока доедет прежняя команда, прежде чем начать новую. Больше
# самой долгой поездки: три метра на скорости ноль двадцать — это пятнадцать
# секунд, плюс запас на разгон.
ОЧЕРЕДЬ_ЖДЁМ = 25.0

# Шасси шлёт напряжение раз в секунду. Десять секунд молчания — связи нет.
VOLTAGE_TTL = 10.0


class Ros:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: websocket.WebSocketApp | None = None
        self._connected = threading.Event()
        self._voltage: float | None = None
        self._voltage_at = 0.0
        self._odom: tuple[float, float, float] | None = None
        self._odom_at = 0.0
        self._lock = threading.Lock()
        self._stop = False
        # Движение крутится в отдельном потоке, иначе робот не услышит «стоп»,
        # пока едет: главный цикл стоял бы в sleep, а микрофон был бы заглушён.
        self._motion_cancel = threading.Event()
        self._motion: threading.Thread | None = None
        # Когда в /cmd_vel последний раз видели ненулевую скорость. Слушаем сам
        # топик, а не только себя: гнать робота может и веб-пульт напрямую.
        self._last_move = 0.0
        # Когда мы сами в последний раз опубликовали ноль. Свои нули
        # прилетают обратно по подписке, и без этой отметки поездка,
        # начатая сразу после предыдущей, отменялась собственным хвостом.
        self._own_zero_at = 0.0

    # --- жизненный цикл -------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._run_forever, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        if self._ws:
            self._ws.close()

    def _run_forever(self) -> None:
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                log.exception("rosbridge: сбой соединения")
            self._connected.clear()
            if not self._stop:
                time.sleep(2)

    def _on_open(self, ws) -> None:
        log.info("rosbridge: подключён к %s", self.url)
        self._connected.set()
        self._send({"op": "advertise", "topic": CMD_VEL, "type": "geometry_msgs/msg/Twist"})
        self._send({"op": "subscribe", "topic": ODOM,
                    "type": "nav_msgs/msg/Odometry", "throttle_rate": 50})
        self._send({"op": "subscribe", "topic": POWER, "type": "std_msgs/msg/Float32",
                    "throttle_rate": 1000})
        # Без throttle намеренно. Это единственный канал, по которому работает
        # кнопка СТОП: rosbridge при throttle не ставит сообщения в очередь, а
        # выбрасывает пришедшие раньше окна, и нули от кнопки терялись целиком
        # — их перекрывало эхо собственной поездки, идущее каждые 66 мс.
        # Разбор трёх десятков json в секунду ничего не стоит.
        self._send({"op": "subscribe", "topic": CMD_VEL, "type": "geometry_msgs/msg/Twist"})

    def _on_close(self, ws, *_a) -> None:
        log.warning("rosbridge: соединение закрыто")
        self._connected.clear()

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("op") != "publish":
            return

        if msg.get("topic") == ODOM:
            self._запомнить_одометрию(msg.get("msg") or {})
            return
        if msg.get("topic") == POWER:
            with self._lock:
                self._voltage = float(msg["msg"]["data"])
                self._voltage_at = time.monotonic()

        elif msg.get("topic") == CMD_VEL:
            try:
                body = msg["msg"]
                speed = (abs(body["linear"]["x"]) + abs(body["linear"]["y"])
                         + abs(body["angular"]["z"]))
            except (KeyError, TypeError):
                return
            if speed > 1e-3:
                with self._lock:
                    self._last_move = time.monotonic()
            elif self.moving and not self._own_zero():
                # Ноль в топике, пока мы едем, — это кто-то другой требует
                # остановиться: кнопка СТОП в пульте или отпущенный джойстик.
                # Без этого поток движения через 66 мс снова подаёт скорость,
                # и кнопка не работает — а она единственная страховка, пока
                # робот говорит и не слышит «стоп».
                log.info("в /cmd_vel пришёл ноль — прекращаю движение")
                self._motion_cancel.set()

    def _send(self, obj: dict) -> None:
        ws = self._ws
        if ws is None or not self._connected.is_set():
            return
        try:
            ws.send(json.dumps(obj))
        except Exception:
            log.warning("rosbridge: не удалось отправить %s", obj.get("op"))

    def wait_connected(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout)

    # --- телеметрия -----------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def voltage(self) -> float | None:
        """Напряжение батареи или None, если свежих данных нет.

        Шасси шлёт его раз в секунду. Если данные протухли — связи с шасси
        нет, и старое значение врёт: иначе робот через сутки после отключения
        уверенно сообщит «батарея двенадцать вольт».
        """
        with self._lock:
            if self._voltage_at and time.monotonic() - self._voltage_at > VOLTAGE_TTL:
                return None
            return self._voltage

    # --- управление -----------------------------------------------------
    def _own_zero(self, window: float = 0.5) -> bool:
        """Наш ли это ноль. Эхо своей же остановки не должно ничего отменять."""
        with self._lock:
            return time.monotonic() - self._own_zero_at < window

    def _запомнить_одометрию(self, сообщение: dict) -> None:
        """Где шасси считает себя сейчас: координаты и поворот."""
        try:
            поза = сообщение["pose"]["pose"]
            точка = поза["position"]
            кватернион = поза["orientation"]
            x, y = float(точка["x"]), float(точка["y"])
            qz, qw = float(кватернион["z"]), float(кватернион["w"])
            qx, qy = float(кватернион.get("x", 0.0)), float(кватернион.get("y", 0.0))
        except (KeyError, TypeError, ValueError):
            return
        # Плоский робот: из кватерниона нужен только поворот вокруг вертикали.
        поворот = math.atan2(2.0 * (qw * qz + qx * qy),
                             1.0 - 2.0 * (qy * qy + qz * qz))
        with self._lock:
            self._odom = (x, y, поворот)
            self._odom_at = time.monotonic()

    @property
    def odom(self) -> tuple[float, float, float] | None:
        """Координаты и поворот. None — шасси про это молчит.

        Молчит оно в двух случаях: узел не запущен или подписка не доехала.
        Оба означают одно — ехать придётся по секундомеру, как раньше.
        """
        with self._lock:
            if not self._odom_at or time.monotonic() - self._odom_at > ODOM_TTL:
                return None
            return self._odom

    def publish_twist(self, x: float = 0.0, y: float = 0.0, wz: float = 0.0) -> None:
        if abs(x) + abs(y) + abs(wz) <= 1e-3:
            with self._lock:
                self._own_zero_at = time.monotonic()
        self._send({
            "op": "publish",
            "topic": CMD_VEL,
            "msg": {
                "linear": {"x": x, "y": y, "z": 0.0},
                "angular": {"x": 0.0, "y": 0.0, "z": wz},
            },
        })

    def stop_motion(self) -> None:
        """Немедленно прекратить движение, чем бы оно ни было вызвано."""
        self.cancel_motion()
        for _ in range(3):      # три раза — на случай потери пакета
            self.publish_twist()
            time.sleep(0.02)

    def cancel_motion(self, timeout: float = 2.0) -> None:
        motion = self._motion
        self._motion_cancel.set()
        if motion is not None and motion.is_alive() and motion is not threading.current_thread():
            motion.join(timeout=timeout)

    @property
    def moving(self) -> bool:
        """Едем ли по своей команде."""
        return self._motion is not None and self._motion.is_alive()

    @property
    def busy(self) -> bool:
        """Движется ли робот вообще — хоть по голосу, хоть с веб-пульта.

        Пульт публикует /cmd_vel напрямую через rosbridge, минуя этот класс,
        поэтому смотрим на сам топик. Полторы секунды — с запасом к 15 Гц,
        с которыми пульт шлёт команды.
        """
        if self.moving:
            return True
        with self._lock:
            last = self._last_move
        return last > 0 and (time.monotonic() - last) < 1.5

    def drive(self, x: float, y: float, wz: float, duration: float,
              *, путь: float = 0.0, угол: float = 0.0) -> None:
        """Запускает движение в отдельном потоке.

        Возврат управления сразу — чтобы робот продолжал слушать и мог
        принять «стоп» на ходу.

        Путь и угол — сколько проехать и на сколько повернуться по правде,
        в метрах и радианах. Если шасси докладывает одометрию, движение
        кончается по ней, а duration остаётся предохранителем на случай, когда
        одометрия врёт или молчит. Без неё всё как раньше — по секундомеру.

        Если робот уже едет — ДОЖИДАЕМСЯ. Раньше новая команда отменяла
        прежнюю, и «проедь вперёд, а потом назад» кончалось одним движением
        назад: модель звала оба инструмента в одну секунду, второй убивал
        первый. Человек видел, что робот дёрнулся и уехал не туда.

        Отмена осталась ровно там, где нужна, — в stop_motion по слову «стоп».
        """
        предыдущее = self._motion
        if (предыдущее is not None and предыдущее.is_alive()
                and предыдущее is not threading.current_thread()):
            # Ждём не бесконечно: если прежнее движение зависло, новое всё
            # равно должно случиться, иначе робот замрёт навсегда.
            предыдущее.join(timeout=ОЧЕРЕДЬ_ЖДЁМ)
        self.cancel_motion()
        self._motion_cancel.clear()
        # Предыдущая поездка только что доложила о себе тремя нулями, и по
        # времени они выглядят как «наши». Если их не забыть, окно в полсекунды
        # накроет начало новой поездки, и кнопка СТОП в первые полсекунды
        # молча не сработает — а проверять её будут на одиночной поездке, где
        # всё хорошо.
        with self._lock:
            self._own_zero_at = 0.0
        self._motion = threading.Thread(
            target=self._motion_loop, args=(x, y, wz, duration),
            kwargs={"путь": путь, "угол": угол}, daemon=True)
        self._motion.start()

    def _motion_loop(self, x: float, y: float, wz: float, duration: float,
                     *, путь: float = 0.0, угол: float = 0.0) -> None:
        # Шасси ждёт /cmd_vel непрерывно — при паузе оно само тормозит,
        # поэтому команду повторяем 15 раз в секунду.
        начало = self.odom if (путь or угол) else None
        if начало is not None:
            # По одометрии ждём дольше: разгон съедает время, и робот доедет
            # позже, чем обещал секундомер. Втрое — с запасом, это всё равно
            # только предохранитель на случай вранья одометрии.
            deadline = time.monotonic() + max(duration * 3.0, duration + 2.0)
        else:
            deadline = time.monotonic() + duration
        пройдено = повёрнуто = 0.0
        прошлый = начало
        try:
            while time.monotonic() < deadline and not self._motion_cancel.is_set():
                self.publish_twist(x, y, wz)
                self._motion_cancel.wait(1 / 15)
                if начало is None:
                    continue
                сейчас = self.odom
                if сейчас is None:
                    continue
                if путь:
                    пройдено = math.hypot(сейчас[0] - начало[0],
                                          сейчас[1] - начало[1])
                    if пройдено >= путь:
                        log.info("проехал %.2f м из %.2f — останавливаюсь",
                                 пройдено, путь)
                        break
                if угол and прошлый is not None:
                    # Складываем приращения, а не считаем разницу с началом:
                    # поворот идёт по кругу, и на трёхстах шестидесяти градусах
                    # разница вернулась бы к нулю.
                    шаг = сейчас[2] - прошлый[2]
                    шаг = (шаг + math.pi) % (2 * math.pi) - math.pi
                    повёрнуто += abs(шаг)
                    if повёрнуто >= угол:
                        log.info("повернулся на %.0f° из %.0f — останавливаюсь",
                                 math.degrees(повёрнуто), math.degrees(угол))
                        break
                прошлый = сейчас
        finally:
            for _ in range(3):
                self.publish_twist()
                time.sleep(0.02)
