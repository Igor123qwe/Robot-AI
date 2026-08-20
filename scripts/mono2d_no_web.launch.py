# Детектор людей на BPU — БЕЗ их странички просмотра.
#
# ЗАЧЕМ СВОЙ ФАЙЛ. Штатный mono2d_body_detection.launch.py поднимает пять
# узлов, и последний из них — web_node, то есть websocket.launch.py, а тот
# запускает NGINX НА ПОРТУ 8000. На 8000 живёт наш пульт.
#
# В комментариях у меня было записано «безвредно, просто заливает вывод
# красным». Это оказалось неправдой, и неправдой дорогой. Пока детектор
# запускали руками на минуту, беда не показывалась: nginx стартовал, пульт в
# это время был остановлен, потом всё возвращалось. А когда детектор включили
# службой навсегда, вышло вот что:
#
#   • nginx занимает 8000 раньше пульта (robot-body стартует Before=robot-web);
#   • пульт не может занять порт, падает с кодом 1 и уходит в вечный
#     перезапуск — «activating (auto-restart), restart counter is at 2»;
#   • в браузере на 192.168.0.51:8000 отвечает «404 Not Found, nginx/1.22.0».
#
# И хуже всего: nginx ДЕМОНИЗИРУЕТСЯ. Он не потомок службы, systemctl stop
# robot-body его не убивает, и он переживает и остановку детектора, и
# перезапуск пульта. Висит, пока не убьёшь руками.
#
# Поэтому мы просто не запускаем его вовсе. Их страничка нам не нужна ни для
# чего: детекции робот берёт из топика, а картинку пульту раздаёт наш
# web_video_server на 8080 (см. scripts/body_service.sh).
#
# Всё остальное — точная копия их файла, ветка usb-камеры, с теми же
# аргументами. Копия, а не импорт: их LaunchDescription собирается целиком, и
# выкинуть из него один узел снаружи нельзя.
#
# Камеру берём usb и не спрашиваем CAM_TYPE. У них при незаданной переменной
# молча выбирается mipi — то есть камера, которой у нас нет, — и разбираться,
# почему детектор видит черноту, пришлось бы заново.

import os

from ament_index_python import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node

ТОПИК = "/hobot_mono2d_body_detection"


def _включить(пакет: str, файл: str, **аргументы):
    источник = PythonLaunchDescriptionSource(
        os.path.join(get_package_share_directory(пакет), "launch", файл))
    return IncludeLaunchDescription(источник, launch_arguments=аргументы.items())


def generate_launch_description():
    # Нулевое копирование между узлами. Без него кадры ходят через ROS и
    # съедают процессор на плате, где он и так наперечёт.
    общая_память = _включить("hobot_shm", "hobot_shm.launch.py")

    # USB-камера. 640×480 — как в их файле: детектор всё равно ужимает кадр до
    # своего входа, а гнать 1920×1200 через кодек значит греть плату впустую.
    камера = _включить("hobot_usb_cam", "hobot_usb_cam.launch.py",
                       usb_image_width="640", usb_image_height="480")

    # jpeg → nv12 в общую память: детектор ест nv12, камера отдаёт jpeg.
    кодек = _включить("hobot_codec", "hobot_codec_decode.launch.py",
                      codec_in_mode="ros", codec_out_mode="shared_mem",
                      codec_sub_topic="/image", codec_pub_topic="/hbmem_img")

    детектор = Node(
        package="mono2d_body_detection",
        executable="mono2d_body_detection",
        output="screen",
        parameters=[{"ai_msg_pub_topic_name": ТОПИК}],
        arguments=["--ros-args", "--log-level", "warn"],
    )

    return LaunchDescription([общая_память, камера, кодек, детектор])
