"""Связь с ROS 2 через rosbridge (websocket, порт 9090).

Прямой rclpy тут не используется намеренно: голосовой пайплайн живёт в своём
venv с faster-whisper, а окружение ROS тянуть в него не хочется. rosbridge уже
поднят для веб-пульта, так что переиспользуем его.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import websocket

log = logging.getLogger(__name__)

CMD_VEL = "/cmd_vel"
POWER = "/PowerVoltage"

# Шасси шлёт напряжение раз в секунду. Десять секунд молчания — связи нет.
VOLTAGE_TTL = 10.0


class Ros:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: websocket.WebSocketApp | None = None
        self._connected = threading.Event()
        self._voltage: float | None = None
        self._voltage_at = 0.0
        self._lock = threading.Lock()
        self._stop = False
        # Движение крутится в отдельном потоке, иначе робот не услышит «стоп»,
        # пока едет: главный цикл стоял бы в sleep, а микрофон был бы заглушён.
        self._motion_cancel = threading.Event()
        self._motion: threading.Thread | None = None
        # Когда в /cmd_vel последний раз видели ненулевую скорость. Слушаем сам
        # топик, а не только себя: гнать робота может и веб-пульт напрямую.
        self._last_move = 0.0

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
        self._send({"op": "subscribe", "topic": POWER, "type": "std_msgs/msg/Float32",
                    "throttle_rate": 1000})
        self._send({"op": "subscribe", "topic": CMD_VEL, "type": "geometry_msgs/msg/Twist",
                    "throttle_rate": 100})

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
            elif self.moving:
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
    def publish_twist(self, x: float = 0.0, y: float = 0.0, wz: float = 0.0) -> None:
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
              block: bool = False) -> None:
        """Запускает движение на заданное время в отдельном потоке.

        Возврат управления сразу — чтобы робот продолжал слушать и мог
        принять «стоп» на ходу. block=True нужен только тестам.
        """
        self.cancel_motion()
        self._motion_cancel.clear()
        self._motion = threading.Thread(
            target=self._motion_loop, args=(x, y, wz, duration), daemon=True)
        self._motion.start()
        if block:
            self._motion.join()

    def _motion_loop(self, x: float, y: float, wz: float, duration: float) -> None:
        # Шасси ждёт /cmd_vel непрерывно — при паузе оно само тормозит,
        # поэтому команду повторяем 15 раз в секунду.
        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline and not self._motion_cancel.is_set():
                self.publish_twist(x, y, wz)
                self._motion_cancel.wait(1 / 15)
        finally:
            for _ in range(3):
                self.publish_twist()
                time.sleep(0.02)
