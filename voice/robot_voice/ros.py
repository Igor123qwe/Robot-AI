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


class Ros:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: websocket.WebSocketApp | None = None
        self._connected = threading.Event()
        self._voltage: float | None = None
        self._lock = threading.Lock()
        self._stop = False

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

    def _on_close(self, ws, *_a) -> None:
        log.warning("rosbridge: соединение закрыто")
        self._connected.clear()

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("op") == "publish" and msg.get("topic") == POWER:
            with self._lock:
                self._voltage = float(msg["msg"]["data"])

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
    def voltage(self) -> float | None:
        with self._lock:
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
        self.publish_twist()

    def drive(self, x: float, y: float, wz: float, duration: float) -> None:
        """Едет заданное время, потом гарантированно останавливается.

        Шасси ждёт /cmd_vel непрерывно — при паузе оно само тормозит,
        поэтому команду повторяем 15 раз в секунду.
        """
        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline:
                self.publish_twist(x, y, wz)
                time.sleep(1 / 15)
        finally:
            # три раза — на случай потери пакета
            for _ in range(3):
                self.stop_motion()
                time.sleep(0.05)
