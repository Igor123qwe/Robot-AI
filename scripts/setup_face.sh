#!/usr/bin/env bash
# Лицо робота на экране: зависимости, права, служба. Запускать от wheeltec.
# Идемпотентно — можно гонять повторно.
#
# Имена переменных — латиницей. Bash допускает в именах только [A-Za-z_0-9],
# и кириллическое «имя=...» он читает как команду, которой нет.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> лицо робота: ставлю зависимости"
# pygame из apt, а не из pip: колесо под aarch64 тянет сборку SDL, а системный
# pygame 2.1 уже собран с SDL2 и умеет kmsdrm. Шрифт — DejaVu, у него есть
# кириллица; без него подпись «слушаю» превратится в квадратики.
#
# `apt-get update` НЕ ЗОВЁМ ПЕРВЫМ ДЕЛОМ, и это урок с живого робота. В
# источниках образа WHEELTEC стоят зеркала D-Robotics, которые из России
# отвечают минутами или никак, а с `-qq` это выглядело как повисший скрипт.
# Порядок: если всё уже стоит — не трогаем apt вовсе; иначе ставим из тех
# списков, что есть; и только если пакета в них нет — обновляем, с пределом
# по времени и с выводом на экран, чтобы было видно, на каком зеркале стоим.
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
have_pygame() { python3 -c "import pygame" 2>/dev/null; }

# --no-install-recommends — не экономия, а необходимость. Без него apt тянет
# libsdl2-mixer, а с ним timgm6mb-soundfont на пять мегабайт из семи: звуковой
# шрифт для микшера, который лицу не нужен вовсе (звук у службы выключен).
# На зеркале Tsinghua из России это 1.4 КБ/с и обрыв по таймауту — проверено.
# Retries — потому что обрывается оно не всегда, и второй раз докачивает.
APT_INSTALL="sudo apt-get install -y --no-install-recommends -o Acquire::Retries=3"

if have_pygame && [ -f "$FONT" ]; then
  echo "==> pygame и шрифт уже стоят, apt не трогаю"
elif $APT_INSTALL python3-pygame fonts-dejavu-core; then
  echo "==> поставил из имеющихся списков"
else
  echo "==> apt не смог (зеркало не отвечает или пакета нет в списках)"
  # Шрифт почти наверняка уже есть — он в базовом образе. Проверяем, а не
  # предполагаем: без него подпись превратится в квадратики.
  [ -f "$FONT" ] || $APT_INSTALL fonts-dejavu-core || true
  # Колесо pygame с PyPI: у него свой CDN, из России он обычно отвечает, и в
  # колесе 2.6 SDL2 собран с kmsdrm. Ставим в пользователя — служба идёт от
  # /usr/bin/python3 под wheeltec и пользовательские пакеты видит.
  echo "==> пробую колесо с PyPI: pip3 install --user pygame"
  if ! python3 -m pip --version >/dev/null 2>&1; then
    $APT_INSTALL python3-pip || true
  fi
  python3 -m pip install --user --disable-pip-version-check pygame
fi

if ! have_pygame; then
  echo "!! pygame так и не встал. Варианты:"
  echo "   1) повторить скрипт — зеркало могло просто отвалиться на минуту;"
  echo "   2) сменить зеркало apt на ближнее (mirror.yandex.ru/ubuntu-ports) —"
  echo "      это правка /etc/apt/sources.list, скажи, и я подготовлю."
  exit 1
fi
if [ ! -f "$FONT" ]; then
  echo "!! шрифта DejaVu нет — кириллица на экране не отрисуется"
  exit 1
fi

echo "==> права на экран"
# DRM (/dev/dri/*) и фреймбуфер выдаются группами video и render. Служба
# работает без входа в систему, а logind свои списки выдаёт только вошедшим —
# та же засада, что с камерой (см. docs/hardware.md).
sudo usermod -aG video,render,input wheeltec

# Проверка, что панель вообще включена. RDK X5 настраивается через srpi-config,
# и пока DSI не выбран там, /dev/dri есть, а картинки нет.
if ! ls /dev/dri/card* >/dev/null 2>&1; then
  echo "!! /dev/dri/card* не найден — DSI-панель не включена."
  echo "   Включить: sudo srpi-config → Display → выбрать панель Waveshare,"
  echo "   потом перезагрузка. После неё запустить этот скрипт ещё раз."
fi

echo "==> отдаю экран лицу: выключаю графический сервер"
# Образ RDK X5 держит на панели X через lightdm, а x11vnc показывает этот X по
# сети. X — DRM-мастер, и пока он жив, лицо экран не откроет (второго хозяина
# у панели не бывает; /dev/fb* на этом ядре нет, запасного пути тоже нет).
# Роботу рабочий стол не нужен: пульт — в браузере, лицо — на панели.
#
# ОБРАТИМО: вернуть рабочий стол — `sudo systemctl disable --now robot-face &&
# sudo systemctl enable --now lightdm x11vnc`.
for unit in lightdm x11vnc; do
  if systemctl list-unit-files "$unit.service" >/dev/null 2>&1 \
     && systemctl list-unit-files "$unit.service" | grep -q "$unit.service"; then
    sudo systemctl disable --now "$unit" || true
    echo "    $unit выключен"
  fi
done

echo "==> служба robot-face"
sudo cp "$REPO/systemd/robot-face.service" /etc/systemd/system/robot-face.service
sudo systemctl daemon-reload
sudo systemctl enable robot-face
sudo systemctl restart robot-face

sleep 2
if systemctl is-active --quiet robot-face; then
  echo "==> robot-face работает. Журнал: journalctl -u robot-face -f"
  echo "    В нём раз в минуту строка «лицо: N к/с» — это проверка №1 из docs/screen.md."
else
  echo "!! robot-face не поднялась. Смотреть: journalctl -u robot-face -n 50"
  exit 1
fi
