#!/usr/bin/env bash
# Поиск вещей: собрать hobot_dosod и положить модель. Запускается один раз.
#
# Что это. DOSOD — открытословарная сеть распознавания предметов от
# D-Robotics, считает на BPU. Восемьдесят классов COCO из коробки, среди них
# много домашнего: кружка, бутылка, книга, рюкзак, ноутбук, телефон, стул,
# диван, кровать, телевизор, кошка, собака.
#
# Готовым пакетом она не приходит — в отличие от детектора людей и жестов, —
# поэтому её надо собрать. Сборка идёт НА ПЛАТЕ: модель под aarch64 и BPU
# конкретно этой ревизии, кросс-сборка тут только добавит способов ошибиться.
#
# Модель весит сорок три мегабайта и лежит в самом репозитории, отдельно
# качать нечего.
#
# ВАЖНО ПРО ЗАГРУЗКУ ПЛАТЫ. Сеть НЕ ставится на живой поток с камеры. Один
# кадр стоит около ста тридцати миллисекунд BPU — это почти вся плата, — и
# постоянная работа отобрала бы её у детектора людей, на котором держатся
# следование, сторож падений и остановка по ладони. Поэтому кадр попадает в
# сеть только по запросу, через затвор scripts/look_relay.py.

set -euo pipefail

TROS="/opt/tros/humble"
WORK_DIR="$HOME/tros_ws"
SRC_DIR="$WORK_DIR/src"

if [ ! -d "$TROS" ]; then
    echo "Нет TogetheROS в $TROS — сначала поставь его" >&2
    exit 1
fi

mkdir -p "$SRC_DIR"
cd "$SRC_DIR"

if [ -d hobot_dosod/.git ]; then
    echo "hobot_dosod уже есть — обновляю"
    git -C hobot_dosod pull --ff-only
else
    git clone --depth 1 https://github.com/D-Robotics/hobot_dosod.git
fi

# Модель и словарь кладём туда же, откуда узел их ищет ОТНОСИТЕЛЬНЫМ путём —
# рядом с рабочим каталогом. Та же ловушка, что у детектора людей: при
# установленной модели узел пишет «Model file is not exist», если запущен из
# другого места.
mkdir -p "$WORK_DIR/config"
cp hobot_dosod/config/x5/dosod_mlp3x_l_rep-int8.bin "$WORK_DIR/config/"
cp hobot_dosod/config/offline_vocabulary.json "$WORK_DIR/config/"

set +u
# shellcheck disable=SC1091
source "$TROS/setup.bash"
set -u

cd "$WORK_DIR"
# --packages-select: собираем ровно один пакет. Полная сборка рабочего
# каталога займёт полчаса и пересоберёт то, что и так работает.
colcon build --packages-select hobot_dosod

echo
echo "Готово. Проверить:"
echo "    source $WORK_DIR/install/setup.bash"
echo "    ros2 pkg list | grep dosod"
echo
echo "Дальше перезапусти детектор, он поднимет и поиск вещей:"
echo "    sudo systemctl restart robot-body"
