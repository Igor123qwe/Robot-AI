#!/usr/bin/env bash
# Поднимает детектор людей на BPU (mono2d_body_detection из TogetheROS).
#
# Зачем он нужен. Следование за человеком ведётся дальномером: восемь колонок
# на шестьдесят градусов. Шаг вбок — и человек вне поля зрения, шесть секунд,
# «Больше не иду». У камеры сто тридцать градусов, и детектор на BPU даёт
# десятки кадров в секунду локально и бесплатно. Облачная модель для этого не
# годится вовсе: она отвечает две-четыре секунды, а решение нужно десять раз в
# секунду.
#
#     bash scripts/body_detect.sh
#
# Скрипт существует потому, что запуск «как в документации» спотыкается о три
# вещи подряд, и каждая выглядит как поломка:
#
#   1. Модель ищется по ОТНОСИТЕЛЬНОМУ пути config/…hbm, то есть рядом с тем
#      каталогом, откуда запускали. Запуск из ~/Robot-AI даёт «Model file is
#      not exist, please install models with apt install!» — при установленных
#      моделях. Совет из сообщения не помогает, потому что беда не в них.
#   2. Камеру держит наш веб-сервер для пульта. hobot_usb_cam перебирает
#      форматы /dev/video0 и умирает с «terminate called after throwing an
#      instance of 'char*'» — без единого слова о том, что устройство занято.
#   3. Их launch поднимает пятым узлом свою страничку просмотра, а та —
#      NGINX НА ПОРТУ 8000, где сидит наш пульт. Здесь было написано
#      «безвредно, просто заливает вывод красным» — неправда, и дорогая:
#      порт он не шумит, а отбирает. Пульт после этого не поднимается вовсе
#      и уходит в вечный перезапуск, а сам nginx демонизируется и переживает
#      остановку детектора. Поэтому запускаем свой launch без их странички.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TROS="/opt/tros/humble"
WORK_DIR="$HOME/tros_ws"
MODEL_DIR="$TROS/lib/mono2d_body_detection/config"
TOPIC="/hobot_mono2d_body_detection"

if [ ! -d "$TROS" ]; then
    echo "Нет $TROS — это не робот с TogetheROS." >&2
    exit 1
fi

echo "== модель"
if [ ! -d "$MODEL_DIR" ]; then
    echo "Нет $MODEL_DIR. Поставь модели:  sudo apt install hobot-models-basic" >&2
    exit 1
fi
HBM="$(find "$MODEL_DIR" -name "multitask_body_head_face_hand_kps*.hbm" | head -1)"
if [ -z "$HBM" ]; then
    echo "В $MODEL_DIR нет модели тела. Обнови:  sudo apt install --only-upgrade hobot-models-basic" >&2
    exit 1
fi
echo "  $HBM"

echo "== рабочий каталог"
# Копируем config рядом, потому что путь к модели относительный. Копия, а не
# ссылка: apt при обновлении моделей заменяет каталог целиком, и ссылка на
# старый повиснет — а повиснет она молча.
mkdir -p "$WORK_DIR"
cp -r "$MODEL_DIR" "$WORK_DIR/"
echo "  $WORK_DIR"

echo "== камера"
# Два процесса на одно USB-устройство не уживаются, и узел камеры об этом не
# говорит — он просто падает. Освобождаем заранее и предупреждаем человека,
# что пульт на это время ослепнет: иначе он пойдёт чинить пульт.
if systemctl is-active --quiet robot-web; then
    echo "  останавливаю robot-web (пульт на это время без картинки)"
    sudo systemctl stop robot-web
    RESTORE_PULT=1
else
    RESTORE_PULT=0
    echo "  robot-web не запущен — камера свободна"
fi

restore_pult() {
    if [ "$RESTORE_PULT" = "1" ]; then
        echo
        echo "возвращаю пульт"
        sudo systemctl start robot-web || true
    fi
}
trap restore_pult EXIT

echo "== поехали"
echo "  топик:  $TOPIC"
echo "  смотреть, что он отдаёт, из другого терминала:"
echo "      ~/Robot-AI/voice/.venv/bin/python scripts/ros_topics.py --слушать $TOPIC --секунд 5"
echo "  их странички просмотра здесь нет намеренно: она поднимает nginx на"
echo "  8000, а там пульт. Смотреть надо на строки mono2d_body_det."
echo

cd "$WORK_DIR"
# Строгий режим снимаем ИМЕННО ЗДЕСЬ и только здесь. Скрипты окружения ROS
# написаны без оглядки на `set -u`: setup.bash первой же строкой читает
# AMENT_TRACE_SETUP_FILES, которой никто не объявлял, и запуск падает с
# «unbound variable» — не дойдя до робота вовсе. Свой код от строгости
# выигрывает, чужой ею ломается.
set +u
# shellcheck disable=SC1091
source "$TROS/setup.bash"
set -u
exec ros2 launch "$REPO/scripts/mono2d_no_web.launch.py"
