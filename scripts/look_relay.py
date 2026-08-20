#!/usr/bin/env python3
"""Затвор для поиска вещей: один кадр по запросу, а не поток круглосуточно.

ЗАЧЕМ ЗАТВОР. Распознавание вещей (DOSOD) — это четвёртая модель на BPU
вдобавок к детектору людей, точкам кисти и жестам. По их же журналу один кадр
стоит около ста тридцати миллисекунд, то есть модель занимает плату почти
целиком. Пустить её на живой поток в тридцать кадров — значит отобрать BPU у
детектора людей, а на нём держатся следование, сторож падений и остановка по
ладони. Менять безопасность на «где моя кружка» нельзя.

Поэтому DOSOD подписан не на камеру, а на наш топик, куда кадр попадает
только когда о нём попросили. Пока не просят — модель не считает ничего.

ЗАЧЕМ ПЕРЕВОД. Камера отдаёт jpeg (`sensor_msgs/CompressedImage`), а DOSOD в
обычном режиме принимает `sensor_msgs/Image` с кодировкой bgr8 или nv12 —
сжатый кадр он не разберёт. Так что переходник понадобился бы и без затвора;
затвор достался бесплатно.

    камера → /image (jpeg) → [мы] → /robot_ai/look_image (bgr8) → DOSOD

Запрос приходит пустым сообщением в /robot_ai/look. Пустым, потому что
единственное, что нужно сказать, — «посмотри сейчас».

ВТОРАЯ ОБЯЗАННОСТЬ: РАЗДАЧА КАРТИНКИ В ПУЛЬТ. Досталась почти бесплатно и
завелась по живой беде.

Раздачей занимался web_video_server из чужой сборки. Он работает, но у службы
своё окружение, чужой слой в нём не подключён, и `ros2 run` падал с «package
not found» — молча, из-за &. Со стороны это семь часов выглядело как «камера
не работает»: детектор в те же секунды исправно вёл людей на тридцати кадрах,
а пульт писал «Connection refused» на 8080.

Раздать кадр отсюда — двадцать строк, потому что мы УЖЕ подписаны на /image, а
там лежит готовый jpeg. Никакого сжатия и разжатия: MJPEG — это те же байты
плюс разделитель между кадрами. Зависимость от чужого слоя сборки при этом
исчезает совсем, а с ней и целый способ сломаться молча.

Адрес нарочно тот же, что был у web_video_server:

    http://127.0.0.1:8080/stream?topic=/image

Менять ничего не надо ни в пульте, ни в настройках. А если настоящий
web_video_server всё-таки поднялся и порт занял — мы отходим в сторону и
говорим об этом в журнал: два раздатчика на одном порту не нужны.
"""

import errno
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import CompressedImage, Image
    from std_msgs.msg import Empty
except ImportError as e:                    # pragma: no cover — только на плате
    print(f"Нет ROS: {e}. Запускать это надо после source /opt/tros/*/setup.bash",
          file=sys.stderr)
    raise SystemExit(1)

ПОРТ_РАЗДАЧИ = int(os.environ.get("ROBOT_STREAM_PORT", "8080"))
# Сколько ждать нового кадра, прежде чем решить, что камера встала. Две
# секунды: камера идёт тридцать кадров в секунду, и две секунды тишины — это
# уже не заминка.
ЖДЁМ_КАДРА = 2.0

КАМЕРА = "/image"
ЗАПРОС = "/robot_ai/look"
КАДР = "/robot_ai/look_image"

# Сколько кадров отдать на один запрос. Три, а не один: камера могла как раз
# моргнуть, робот мог качнуться на повороте, а второй попытки человек не
# делает — он спросил один раз. Три кадра стоят четырёх десятых секунды BPU.
КАДРОВ_НА_ЗАПРОС = 3


def _разжать(данные):
    """jpeg → массив bgr8. None — разжать нечем.

    cv2 приходит вместе с cv_bridge, то есть он на плате есть почти наверняка.
    Но «почти» здесь мало: без запасного пути отсутствие одной библиотеки
    молча превратило бы поиск вещей в вечное «ничего не вижу».
    """
    try:
        import cv2
        import numpy as np
        кадр = cv2.imdecode(np.frombuffer(данные, dtype=np.uint8),
                            cv2.IMREAD_COLOR)
        return кадр if кадр is not None else None
    except ImportError:
        pass
    try:
        import io

        import numpy as np
        from PIL import Image as PILImage
        картинка = PILImage.open(io.BytesIO(данные)).convert("RGB")
        # PIL отдаёт RGB, а DOSOD ждёт bgr8. Перепутать порядок каналов —
        # значит кормить модель синими людьми и удивляться, что она молчит.
        return np.asarray(картинка)[:, :, ::-1].copy()
    except ImportError:
        return None


class Затвор(Node):

    def __init__(self) -> None:
        super().__init__("robot_ai_look_relay")
        self._последний = None
        self._jpeg = None
        self._когда_кадр = 0.0
        self._замок = threading.Lock()
        self._осталось = 0
        self._жаловались = False
        self.create_subscription(CompressedImage, КАМЕРА, self._кадр, 1)
        self.create_subscription(Empty, ЗАПРОС, self._просят, 1)
        self._выход = self.create_publisher(Image, КАДР, 1)
        self.get_logger().info(
            f"затвор: жду запрос в {ЗАПРОС}, кадр отдам в {КАДР}")

    def последний_jpeg(self):
        """Последний кадр с камеры как есть, сжатым. None — камера молчит.

        Свежесть проверяем ЗДЕСЬ, а не у того, кто спрашивает: раздача не
        должна показывать в пульте картинку двухминутной давности как живую.
        Застывший кадр хуже пустого экрана — по нему принимают решения.
        """
        with self._замок:
            кадр, когда = self._jpeg, self._когда_кадр
        if кадр is None or time.monotonic() - когда > ЖДЁМ_КАДРА:
            return None
        return кадр

    def _кадр(self, сообщение) -> None:
        # Храним СЖАТЫЙ кадр и не разжимаем ничего, пока не попросят: разжимать
        # тридцать кадров в секунду впустую — это тот же расход, от которого мы
        # и уходим, только на процессоре вместо BPU.
        self._последний = сообщение
        # Копию для раздачи держим отдельно и под замком: её читает поток
        # HTTP-сервера, а этот метод зовёт поток ROS.
        with self._замок:
            self._jpeg = bytes(сообщение.data)
            self._когда_кадр = time.monotonic()
        if self._осталось > 0:
            self._осталось -= 1
            self._отдать(сообщение)

    def _просят(self, _сообщение) -> None:
        self._осталось = КАДРОВ_НА_ЗАПРОС
        # Первый кадр отдаём сразу из запаса, не дожидаясь следующего с
        # камеры: между кадрами тридцатая доля секунды, но если камера как раз
        # встала, ждать пришлось бы вечно и молча.
        if self._последний is not None:
            self._осталось -= 1
            self._отдать(self._последний)

    def _отдать(self, сжатый) -> None:
        кадр = _разжать(bytes(сжатый.data))
        if кадр is None:
            if not self._жаловались:
                self._жаловались = True
                self.get_logger().error(
                    "нечем разжать jpeg: нет ни cv2, ни PIL. "
                    "Поиск вещей работать не будет")
            return
        высота, ширина = кадр.shape[:2]
        сообщение = Image()
        сообщение.header = сжатый.header
        сообщение.height = высота
        сообщение.width = ширина
        сообщение.encoding = "bgr8"
        сообщение.is_bigendian = 0
        сообщение.step = ширина * 3
        сообщение.data = кадр.tobytes()
        self._выход.publish(сообщение)


class _Раздача(BaseHTTPRequestHandler):
    """MJPEG наружу. Кадр берётся из узла как есть, сжатым.

    Отдаём ровно те байты, что пришли с камеры. Разжимать и сжимать обратно —
    это тратить процессор на то, чтобы получить тот же jpeg, только хуже.
    """

    узел = None                      # ставится при запуске
    protocol_version = "HTTP/1.0"

    def log_message(self, формат, *аргументы):
        # Молчим: иначе каждый кадр уходит строкой в журнал службы, а их
        # тридцать в секунду.
        pass

    def _кадр_или_ошибка(self):
        кадр = self.узел.последний_jpeg() if self.узел else None
        if кадр is None:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "Кадра нет: камера молчит дольше двух секунд.\n"
                "Смотри journalctl -u robot-body — жив ли hobot_usb_cam."
                .encode())
            return None
        return кадр

    def do_GET(self):                # noqa: N802 — имя задано базовым классом
        путь = self.path.split("?", 1)[0].rstrip("/")
        if путь in ("/snapshot", "/image"):
            кадр = self._кадр_или_ошибка()
            if кадр is None:
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(кадр)))
            self.end_headers()
            self.wfile.write(кадр)
            return
        if путь not in ("/stream", ""):
            self.send_response(404)
            self.end_headers()
            return
        # Поток. Заголовок тот же, что у web_video_server, чтобы пульт и
        # браузер не заметили подмены.
        if self._кадр_или_ошибка() is None:
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                кадр = self.узел.последний_jpeg() if self.узел else None
                if кадр is None:
                    return
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    f"Content-Length: {len(кадр)}\r\n\r\n".encode())
                self.wfile.write(кадр)
                self.wfile.write(b"\r\n")
                # Тридцать кадров в секунду отдавать незачем: пульт смотрят
                # глазами, а каждый лишний кадр — это байты по вайфаю.
                time.sleep(1 / 15)
        except (BrokenPipeError, ConnectionResetError):
            # Пульт закрыли — обычное дело, а не беда.
            return


def поднять_раздачу(узел, порт: int = ПОРТ_РАЗДАЧИ):
    """Запустить HTTP-раздачу. Отдаёт сервер или None, если порт занят.

    Занятый порт — не ошибка: значит настоящий web_video_server всё-таки
    поднялся, и два раздатчика на одном порту не нужны. Отходим в сторону и
    говорим об этом.
    """
    _Раздача.узел = узел
    try:
        сервер = ThreadingHTTPServer(("0.0.0.0", порт), _Раздача)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            узел.get_logger().info(
                f"порт {порт} уже занят — раздаёт кто-то другой, не мешаю")
        else:
            узел.get_logger().warning(f"раздача не поднялась: {e}")
        return None
    сервер.daemon_threads = True
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    узел.get_logger().info(
        f"раздаю картинку: http://127.0.0.1:{порт}/stream?topic=/image")
    return сервер


def main() -> None:
    rclpy.init()
    узел = Затвор()
    поднять_раздачу(узел)
    try:
        rclpy.spin(узел)
    except KeyboardInterrupt:
        pass
    finally:
        узел.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
