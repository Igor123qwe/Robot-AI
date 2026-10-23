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

# Может оказаться, что собирать нечего: в свежих сборках TogetheROS DOSOD уже
# приходит готовым пакетом. Тогда нужна только модель рядом с рабочим
# каталогом, а её кладёт body_service.sh при каждом запуске.
set +u
# shellcheck disable=SC1091
source "$TROS/setup.bash"
set -u
# СПЕРВА УБЕДИМСЯ, ЧТО СПРАШИВАТЬ ЕСТЬ У КОГО.
#
# Здесь стояло `ros2 pkg list 2>/dev/null | grep -qx hobot_dosod`, и у этой
# проверки два разных исхода выглядели одинаково: «пакета нет» и «ros2 не
# ответил». Второй случай молча уводил в ветку сборки — а она клонирует
# семьдесят пять мегабайт и собирает поверх того, что уже лежит в
# /opt/tros/humble. Colcon на это честно ругается:
#
#     'hobot_dosod' is in: /opt/tros/humble
#     If a package in a merged underlay workspace is overridden ...
#     undefined behavior at run time
#
# То есть цена ошибки — не потерянные полчаса, а перекрытый пакет, который
# однажды поведёт себя не так, как отлаженный. Спрашиваем прямо и проверяем,
# что ответ вообще был.
PKG_LIST="$(ros2 pkg list 2>/dev/null || true)"
if [ -z "$PKG_LIST" ]; then
    echo "ros2 не отвечает: 'ros2 pkg list' вернул пустоту." >&2
    echo "Собирать вслепую нельзя — можно перекрыть готовый пакет." >&2
    echo "Проверь окружение:  source $TROS/setup.bash && ros2 pkg list | head" >&2
    exit 1
fi
if printf '%s\n' "$PKG_LIST" | grep -qx "hobot_dosod"; then
    echo "hobot_dosod уже есть в TogetheROS — собирать нечего."
    echo "А вот МОДЕЛИ у пакета из репозитория может не быть: деб-пакет её"
    echo "не несёт, и узел тогда пишет «Model file is not exist». Кладём."
    if [ ! -f "$WORK_DIR/config/dosod_mlp3x_l_rep-int8.bin" ]; then
        mkdir -p "$SRC_DIR" "$WORK_DIR/config"
        cd "$SRC_DIR"
        if [ -d hobot_dosod/.git ]; then
            git -C hobot_dosod pull --ff-only || true
        else
            git clone --depth 1 https://github.com/D-Robotics/hobot_dosod.git
        fi
        cp hobot_dosod/config/x5/dosod_mlp3x_l_rep-int8.bin "$WORK_DIR/config/"
        cp hobot_dosod/config/offline_vocabulary.json "$WORK_DIR/config/"
        echo "модель и словарь положены в $WORK_DIR/config"
    else
        echo "модель уже лежит в $WORK_DIR/config — трогать не буду"
    fi
    echo
    echo
    echo "Перезапусти детектор и посмотри, что он скажет про поиск вещей:"
    echo "    sudo systemctl restart robot-body"
    echo "    journalctl -u robot-body -n 40 --no-pager | grep -i 'вещ\\|dosod'"
    exit 0
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
