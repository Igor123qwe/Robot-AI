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
sudo apt-get update -qq
sudo apt-get install -y python3-pygame fonts-dejavu-core

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
