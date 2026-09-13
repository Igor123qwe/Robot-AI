#!/usr/bin/env bash
# Детектор людей на BPU плюс раздача картинки всем остальным. Запускается
# службой robot-body и работает постоянно.
#
# Постоянно — потому что в этом весь смысл. Детектор нужен не по команде «иди
# за мной», а всё время: он даёт пеленг человека следованию, он же потом
# понадобится для «кто в комнате» и «где кошка». Тридцать кадров в секунду на
# BPU стоят десяти миллисекунд каждый и не мешают ничему.
#
# Но USB-камеру может держать ТОЛЬКО ОДИН процесс, и с этого момента её держит
# hobot_usb_cam. Значит пульт и зрение обязаны брать картинку у него, а не
# ломиться в /dev/video0 — иначе они ослепнут насовсем. Раздачей занимается
# наш затвор look_relay.py: он и так подписан на /image ради распознавания
# вещей, а раздать те же байты потоком MJPEG стоит двадцати строк — сжатие уже
# сделано камерой. Адрес тот же, что был у web_video_server, поэтому
# ROBOT_CAMERA_URL в ~/.robot-ai.env менять не надо.
#
# Порядок такой:
#     hobot_usb_cam  →  /image  →  mono2d_body_detection  →  детекции
#                            └────→  look_relay.py        →  пульт и зрение
#                                                         └→  DOSOD по запросу

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TROS="/opt/tros/humble"
WORK_DIR="$HOME/tros_ws"
MODEL_DIR="$TROS/lib/mono2d_body_detection/config"

if [ ! -d "$MODEL_DIR" ]; then
    echo "Нет моделей для BPU. Поставь:  sudo apt install hobot-models-basic" >&2
    exit 1
fi

# Config копируем рядом с рабочим каталогом: путь к модели внутри узла
# ОТНОСИТЕЛЬНЫЙ, и запуск из чужого места даёт «Model file is not exist» при
# установленных моделях. Копия, а не ссылка: apt заменяет каталог целиком.
#
# Каталогов теперь три: к детектору людей добавились узлы жестов, и у них та
# же ловушка с относительным путём. Копируем СОДЕРЖИМОЕ каждого в один общий
# config — так велит и документация D-Robotics. Именно содержимое, а не сам
# каталог: `cp -r .../config "$WORK_DIR/"` вторым разом положил бы
# config/config, и модель опять «не нашлась» бы.
mkdir -p "$WORK_DIR/config"
for pkg in mono2d_body_detection hand_lmk_detection hand_gesture_detection \
           hobot_dosod; do
    # Каталог у пакета бывает и в lib, и в share — зависит от того, пришёл он
    # деб-пакетом или собран нами в свой слой. Берём тот, что нашёлся.
    for src in "$TROS/lib/$pkg/config" "$TROS/share/$pkg/config" \
               "$WORK_DIR/src/$pkg/config"; do
        [ -d "$src" ] || continue
        cp -r "$src/." "$WORK_DIR/config/"
        break
    done
done

# Модель DOSOD в их репозитории лежит в подкаталоге по имени платы
# (config/x5/...), а узлу мы даём путь относительно рабочего каталога.
# Поднимаем её на уровень выше, чтобы путь был один и тот же независимо от
# того, откуда пакет взялся.
if [ -f "$WORK_DIR/config/x5/dosod_mlp3x_l_rep-int8.bin" ] \
   && [ ! -f "$WORK_DIR/config/dosod_mlp3x_l_rep-int8.bin" ]; then
    cp "$WORK_DIR/config/x5/dosod_mlp3x_l_rep-int8.bin" "$WORK_DIR/config/"
fi

# Не молча. Человек, машущий роботу ладонью, должен узнать причину отсюда, а
# не гадать, почему тот едет дальше.
[ -d "$TROS/lib/hand_gesture_detection/config" ] \
    || echo "ЖЕСТОВ МОЖЕТ НЕ БЫТЬ: нет config у hand_gesture_detection" >&2
cd "$WORK_DIR"

# Строгий режим снимаем только на подключение окружения: скрипты ROS написаны
# без оглядки на `set -u`, и setup.bash падает на необъявленной переменной.
set +u
# СНАЧАЛА РАБОЧИЙ КАТАЛОГ WHEELTEC, и вот почему это отдельная строчка.
#
# ЗАЧЕМ ЗДЕСЬ ЕЩЁ КАТАЛОГ WHEELTEC, если раздачу мы у него забрали. Затем, что
# оттуда приходит не только она: там же лежит драйвер шасси. А сам
# web_video_server — тот узел, что РАНЬШЕ раздавал картинку в пульт, — приходит
# НЕ с TogetheROS и НЕ из нашего слоя, а из сборки wheeltec:
#
#     /home/wheeltec/wheeltec_ros2/install/web_video_server
#
# В оболочке человека он виден, потому что .bashrc сорсит этот каталог. А
# служба systemd живёт своим окружением, его не сорсила — и `ros2 run
# web_video_server` падал с «package not found». Молча, из-за &.
#
# Со стороны это выглядело как «камера не работает»: детектор при этом
# семь часов исправно вёл людей, а пульт писал «Connection refused» на 8080.
#
# Каталог ищем, а не прибиваем путём: у разных сборок он зовётся по-разному, а
# приметой служит сам пакет. Сорсим ПЕРВЫМ, чтобы наши слои — TogetheROS и
# свой — легли поверх и имели приоритет над всем, что там может совпасть.
for ws in "$HOME/wheeltec_ros2" "$HOME/ros2_ws" "$HOME/catkin_ws"; do
    if [ -d "$ws/install/web_video_server" ] \
       && [ -f "$ws/install/setup.bash" ]; then
        # shellcheck disable=SC1091
        source "$ws/install/setup.bash"
        echo "раздача картинки берётся из $ws"
        break
    fi
done
# shellcheck disable=SC1091
source "$TROS/setup.bash"
# Поверх — свой слой сборки, если он есть. Там живёт hobot_dosod (поиск
# вещей): готовым пакетом он не приходит и собирается отдельно, см.
# scripts/setup_things.sh. Нет слоя — работаем без поиска вещей.
if [ -f "$WORK_DIR/install/setup.bash" ]; then
    # shellcheck disable=SC1091
    source "$WORK_DIR/install/setup.bash"
fi
set -u

# РАЗДАЧУ КАРТИНКИ ВЕДЁТ НАШ ЗАТВОР, А НЕ web_video_server.
#
# Здесь запускался web_video_server из сборки wheeltec. Он ЗАПУСКАЛСЯ и жил, но
# не раздавал ничего — а поскольку порт при этом занимал, наш собственный
# раздатчик (scripts/look_relay.py) видел «порт занят», уходил в сторону и
# честно писал в журнал «не мешаю». Картинку в итоге не отдавал никто.
#
# Разобрано по живому роботу, и цепочка стоит того, чтобы её записать:
#
#     ros2 node list | grep web_video   ->  /web_video_server        (жив)
#     curl .../stream?topic=/image      ->  --boundarydonotcross     (и тишина)
#     curl .../                         ->  список топиков ПУСТ
#     ros2 topic info /image --verbose  ->  издатель шлёт CompressedImage,
#                                           а web_video_server подписан на Image
#
# Он ждёт сырую картинку, а hobot_usb_cam кладёт в /image готовый jpeg. Мы даже
# подставили ему сырой топик через image_transport republish — не помогло:
# список топиков остался пуст, кадров не появилось. Сборка нерабочая.
#
# Проверка тут была, но не та: `kill -0` ловит СМЕРТЬ процесса, а этот жил.
# Живой и бесполезный для неё неотличим от живого и работающего.
#
# Своя раздача лучше по существу, а не только потому, что эта сломалась:
#
#   не жмёт и не разжимает — в /image уже jpeg, а MJPEG это те же байты плюс
#     разделитель, тогда как web_video_server декодировал бы и кодировал снова;
#   не зависит от чужого слоя сборки, которого нет в окружении службы, — на
#     этом уже был потерян вечер («package not found» молча, из-за &);
#   поднимается тем же launch-файлом, что и всё остальное, и падает вместе с
#     ним, а не живёт отдельной жизнью.
#
# Адрес не меняется: look_relay слушает тот же порт и тот же путь, поэтому ни
# в пульте, ни в ~/.robot-ai.env править нечего.
RAZDACHA=""

stop_all() {
    [ -n "$RAZDACHA" ] && kill "$RAZDACHA" 2>/dev/null || true
}
trap stop_all EXIT

# Запускаем СВОЙ launch, а не их. Их поднимает пятым узлом websocket, а тот
# — nginx на порту 8000, где живёт пульт. Подробности и разбор — в шапке
# scripts/mono2d_no_web.launch.py.
exec ros2 launch "$REPO/scripts/mono2d_no_web.launch.py"
