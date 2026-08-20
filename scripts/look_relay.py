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
"""

import sys

try:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import CompressedImage, Image
    from std_msgs.msg import Empty
except ImportError as e:                    # pragma: no cover — только на плате
    print(f"Нет ROS: {e}. Запускать это надо после source /opt/tros/*/setup.bash",
          file=sys.stderr)
    raise SystemExit(1)

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
        self._осталось = 0
        self._жаловались = False
        self.create_subscription(CompressedImage, КАМЕРА, self._кадр, 1)
        self.create_subscription(Empty, ЗАПРОС, self._просят, 1)
        self._выход = self.create_publisher(Image, КАДР, 1)
        self.get_logger().info(
            f"затвор: жду запрос в {ЗАПРОС}, кадр отдам в {КАДР}")

    def _кадр(self, сообщение) -> None:
        # Храним СЖАТЫЙ кадр и не разжимаем ничего, пока не попросят: разжимать
        # тридцать кадров в секунду впустую — это тот же расход, от которого мы
        # и уходим, только на процессоре вместо BPU.
        self._последний = сообщение
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


def main() -> None:
    rclpy.init()
    узел = Затвор()
    try:
        rclpy.spin(узел)
    except KeyboardInterrupt:
        pass
    finally:
        узел.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
