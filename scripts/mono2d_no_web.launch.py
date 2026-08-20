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
import sys

from ament_index_python import get_package_share_directory
from launch import LaunchDescription
from launch.actions import ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node

ТОПИК = "/hobot_mono2d_body_detection"
ТОЧКИ_КИСТИ = "/hobot_hand_lmk_detection"
ЖЕСТЫ = "/hobot_hand_gesture_detection"


def _есть(пакет: str) -> bool:
    """Установлен ли пакет. Нет — запуск обязан продолжиться без него.

    Узел, которого нет, роняет ВЕСЬ launch: ros2 не находит исполняемый файл и
    прекращает запуск целиком. То есть отсутствие жестов унесло бы вместе с
    собой и детектор людей, а значит следование, пеленг и сторожа падений.
    """
    try:
        get_package_share_directory(пакет)
        return True
    except Exception:                       # noqa: BLE001
        return False


def _рядом(*имена: str) -> str:
    """Первый из путей, который существует рядом с рабочим каталогом.

    Путь к модели узел разбирает ОТНОСИТЕЛЬНО того места, откуда запущен, а
    сами файлы приходят по-разному: деб-пакетом они лежат в подкаталоге по
    имени платы (config/x5/...), нашей сборкой — прямо в config. Проверять
    оба варианта дешевле, чем выяснять на роботе, почему узел не нашёл файл,
    который лежит на месте.
    """
    for имя in имена:
        if os.path.exists(имя):
            return имя
    return ""


def _модель_dosod() -> str:
    return _рядом("config/dosod_mlp3x_l_rep-int8.bin",
                  "config/x5/dosod_mlp3x_l_rep-int8.bin")


def _словарь_dosod() -> str:
    return _рядом("config/offline_vocabulary.json",
                  "config/x5/offline_vocabulary.json")


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

    # Жесты рукой: два узла поверх детектора людей. Первый ищет двадцать одну
    # точку кисти в рамках рук, которые детектор уже нашёл, второй по этим
    # точкам называет жест. Через жест «ладонь» работает остановка — путь к
    # тормозам, не проходящий через микрофон.
    #
    # ИХ ЗАПУСК ЗДЕСЬ ИСПОЛЬЗОВАТЬ НЕЛЬЗЯ, и это та же грабля, что уже стоила
    # нам вечера. hand_lmk_detection.launch.py первым делом подключает штатный
    # mono2d_body_detection.launch.py — тот самый, который поднимает websocket
    # и nginx на порту 8000. Вдобавок это был бы ВТОРОЙ mono2d и вторая
    # попытка захватить USB-камеру, которую держать может только один процесс.
    # Поэтому берём из их пакетов ровно по узлу и подключаем к нашей цепочке.
    #
    # ВЫКЛЮЧЕНЫ ПО УМОЛЧАНИЮ. Включает ROBOT_GESTURES=1 в ~/.robot-ai.env — та
    # же переменная, что и в config.py. Одной половины выключателя мало: если
    # погасить только слушателя, плата продолжит считать две сети тридцать раз
    # в секунду, а результат никто не прочтёт. Если погасить только узлы —
    # робот будет ждать топика, которого нет.
    жесты = []
    хотим_жесты = os.environ.get("ROBOT_GESTURES", "0").strip() == "1"
    if not хотим_жесты:
        print("ЖЕСТЫ ВЫКЛЮЧЕНЫ. Включить: ROBOT_GESTURES=1 в ~/.robot-ai.env")
    elif _есть("hand_lmk_detection") and _есть("hand_gesture_detection"):
        жесты = [
            Node(
                package="hand_lmk_detection",
                executable="hand_lmk_detection",
                output="screen",
                parameters=[{"ai_msg_pub_topic_name": ТОЧКИ_КИСТИ},
                            {"ai_msg_sub_topic_name": ТОПИК}],
                arguments=["--ros-args", "--log-level", "warn"],
            ),
            Node(
                package="hand_gesture_detection",
                executable="hand_gesture_detection",
                output="screen",
                parameters=[{"ai_msg_pub_topic_name": ЖЕСТЫ},
                            {"ai_msg_sub_topic_name": ТОЧКИ_КИСТИ},
                            # Статические жесты: ладонь, палец вверх, «тише».
                            # Динамические (щипки и круги) нам не нужны и
                            # требуют накопления кадров, то есть задержки.
                            {"is_dynamic_gesture": False},
                            # Окно голосования за жест. Четверть секунды —
                            # их же умолчание.
                            {"time_interval_sec": 0.25}],
                arguments=["--ros-args", "--log-level", "warn"],
            ),
        ]
    else:
        # Не молча. Робот без жестов работает как работал, но человек, который
        # машет ему ладонью и не понимает, почему тот едет дальше, должен
        # узнать причину из журнала, а не гадать.
        # Отдельным пакетом они не ставятся — приходят вместе с TogetheROS.
        # Поэтому не выдумываем apt-команду, а говорим, чем проверить.
        print("ЖЕСТОВ НЕ БУДЕТ: нет hand_lmk_detection / "
              "hand_gesture_detection. Проверь: ros2 pkg list | grep hand")

    # Поиск вещей: сеть DOSOD плюс наш затвор. Затвор берёт jpeg с камеры,
    # разжимает его в bgr8 и отдаёт сети — но только когда о кадре попросили.
    # Постоянно её пускать нельзя: один кадр стоит около ста тридцати
    # миллисекунд BPU, и она отобрала бы плату у детектора людей, на котором
    # держатся следование, падения и остановка по ладони.
    #
    # Пакет собирается отдельно (scripts/setup_things.sh) и живёт не в
    # /opt/tros, а в рабочем каталоге — поэтому его может и не быть.
    # ЗАТВОР ЗАПУСКАЕТСЯ ВСЕГДА, а не вместе с DOSOD, и это исправление живой
    # беды. У него две работы, и вторая нужна независимо от первой:
    #
    #   1. отдавать кадр по запросу для поиска вещей — только при DOSOD;
    #   2. РАЗДАВАТЬ КАРТИНКУ В ПУЛЬТ на 8080 — нужно всегда.
    #
    # Раньше он стоял внутри «если есть DOSOD и модель», и когда их нет, пульт
    # оставался без картинки вовсе. Со стороны это выглядит как «камера не
    # работает», хотя камера цела и детектор ведёт людей на тридцати кадрах.
    #
    # Без DOSOD затвор просто публикует кадры, которых никто не слушает, —
    # это ничего не стоит: без запроса он не разжимает ни одного кадра.
    затвор = [
        ExecuteProcess(
            cmd=[sys.executable,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "look_relay.py")],
            output="screen",
        ),
    ]

    вещи = []
    модель = _модель_dosod()
    if _есть("hobot_dosod") and модель:
        вещи = [
            Node(
                package="hobot_dosod",
                executable="hobot_dosod",
                output="screen",
                parameters=[
                    {"feed_type": 0},
                    # Обычный ros-режим, а не общая память: затвор отдаёт
                    # обычный sensor_msgs/Image, и это ровно то, чего мы
                    # хотим — кадр по запросу, а не поток.
                    {"is_shared_mem_sub": 0},
                    {"ros_img_sub_topic_name": "/robot_ai/look_image"},
                    {"ai_msg_pub_topic_name": "/perception/detection/dosod"},
                    {"model_file_name": модель},
                    {"vocabulary_file_name": _словарь_dosod()},
                    # Порог у них по умолчанию 0.2 — для картинки в отладчике
                    # сойдёт, для ответа человеку мало. Отсев по уверенности
                    # есть и у нас (things.ВЕРИМ_ОТ), но чем меньше мусора
                    # доедет до разбора, тем лучше.
                    {"score_threshold": 0.3},
                ],
                arguments=["--ros-args", "--log-level", "warn"],
            ),
        ]
    elif not _есть("hobot_dosod"):
        print("ПОИСКА ВЕЩЕЙ НЕ БУДЕТ: нет hobot_dosod. "
              "Собери: bash scripts/setup_things.sh")
    else:
        # Пакет есть, а модели рядом нет. Поднимать узел в таком виде нельзя:
        # он упадёт с «Model file is not exist», launch перезапустит его по
        # кругу и зальёт журнал, в котором мы потом не найдём ничего другого.
        print(f"ПОИСКА ВЕЩЕЙ НЕ БУДЕТ: нет модели рядом с {os.getcwd()}. "
              "Её кладёт scripts/body_service.sh — проверь его вывод.")

    return LaunchDescription(
        [общая_память, камера, кодек, детектор] + жесты + затвор + вещи)
