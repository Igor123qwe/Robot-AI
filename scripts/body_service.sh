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
for pkg in mono2d_body_detection hand_lmk_detection hand_gesture_detection; do
    src="$TROS/lib/$pkg/config"
    if [ -d "$src" ]; then
        cp -r "$src/." "$WORK_DIR/config/"
    elif [ "$pkg" != "mono2d_body_detection" ]; then
        # Не молча: человек, машущий роботу ладонью, должен узнать причину
        # отсюда, а не гадать, почему тот едет дальше.
        echo "ЖЕСТОВ НЕ БУДЕТ: нет $src" >&2
    fi
done
cd "$WORK_DIR"

# Строгий режим снимаем только на подключение окружения: скрипты ROS написаны
# без оглядки на `set -u`, и setup.bash падает на необъявленной переменной.
set +u
# shellcheck disable=SC1091
source "$TROS/setup.bash"
set -u

# Раздача картинки — своим процессом, чтобы падение одного не уносило другое.
# Порт 8080: пульт сидит на 8000, и трогать его нельзя.
#
# Раньше здесь было написано «websocket-просмотрщик TogetheROS на 8000 же и
# лезет — с ним мы не спорим, он нам не нужен». Не спорить с ним было нельзя:
# он этот порт ОТБИРАЕТ, пульт после этого не поднимается вовсе, а nginx
# демонизируется и переживает остановку детектора. Теперь мы его просто не
# запускаем — см. scripts/mono2d_no_web.launch.py.
ros2 run web_video_server web_video_server --ros-args \
    -p port:=8080 -p address:=0.0.0.0 &
RAZDACHA=$!

stop_all() {
    kill "$RAZDACHA" 2>/dev/null || true
}
trap stop_all EXIT

# Запускаем СВОЙ launch, а не их. Их поднимает пятым узлом websocket, а тот
# — nginx на порту 8000, где живёт пульт. Подробности и разбор — в шапке
# scripts/mono2d_no_web.launch.py.
exec ros2 launch "$REPO/scripts/mono2d_no_web.launch.py"
