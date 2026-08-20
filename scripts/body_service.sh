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
# web_video_server: он отдаёт тот же ROS-топик обычным потоком MJPEG по HTTP,
# который наш веб-сервер уже умеет читать (ROBOT_CAMERA_URL в ~/.robot-ai.env).
#
# Порядок такой:
#     hobot_usb_cam  →  /image  →  mono2d_body_detection  →  детекции
#                            └────→  web_video_server     →  пульт и зрение

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

# Раздача картинки — своим процессом, чтобы падение одного не уносило другое.
# Порт 8080: пульт сидит на 8000, и трогать его нельзя.
#
# Раньше здесь было написано «websocket-просмотрщик TogetheROS на 8000 же и
# лезет — с ним мы не спорим, он нам не нужен». Не спорить с ним было нельзя:
# он этот порт ОТБИРАЕТ, пульт после этого не поднимается вовсе, а nginx
# демонизируется и переживает остановку детектора. Теперь мы его просто не
# запускаем — см. scripts/mono2d_no_web.launch.py.
# ПРОВЕРЯЕМ, ЧТО РАЗДАЧУ ВООБЩЕ ЕСТЬ ЧЕМ ЗАПУСТИТЬ. Раньше здесь стоял голый
# `ros2 run ... &`, и когда пакета не оказывалось, он падал МОЛЧА: символ &
# отправляет процесс в фон, а код возврата никто не смотрит.
#
# Со стороны это выглядит как «камера не работает»: детектор при этом
# исправно ведёт людей, а пульт показывает
#
#     http://127.0.0.1:8080/stream?topic=/image: Connection refused
#
# и человек ищет неисправность в камере, которой нет. Вечер потерян на то,
# чтобы узнать: не установлен один пакет.
if ! ros2 pkg prefix web_video_server >/dev/null 2>&1; then
    echo "ВНИМАНИЕ: пакета web_video_server нет — картинки в пульте не будет." >&2
    echo "Детектор людей при этом работает: камеру он берёт из ROS-топика," >&2
    echo "а не из /dev/video0, и раздача ему не нужна." >&2
    echo "Поставить:  sudo apt install ros-\${ROS_DISTRO}-web-video-server" >&2
    RAZDACHA=""
else
    ros2 run web_video_server web_video_server --ros-args \
        -p port:=8080 -p address:=0.0.0.0 &
    RAZDACHA=$!
    # Дать ему подняться и убедиться, что он и правда слушает. Процесс,
    # который стартовал и тут же умер, для `&` неотличим от живого.
    sleep 2
    if ! kill -0 "$RAZDACHA" 2>/dev/null; then
        echo "ВНИМАНИЕ: раздача картинки не поднялась — смотри выше, что она" >&2
        echo "написала. Остальное работает: детектор берёт кадры из топика." >&2
        RAZDACHA=""
    else
        echo "раздача картинки: http://127.0.0.1:8080/stream?topic=/image"
    fi
fi

stop_all() {
    [ -n "$RAZDACHA" ] && kill "$RAZDACHA" 2>/dev/null || true
}
trap stop_all EXIT

# Запускаем СВОЙ launch, а не их. Их поднимает пятым узлом websocket, а тот
# — nginx на порту 8000, где живёт пульт. Подробности и разбор — в шапке
# scripts/mono2d_no_web.launch.py.
exec ros2 launch "$REPO/scripts/mono2d_no_web.launch.py"
