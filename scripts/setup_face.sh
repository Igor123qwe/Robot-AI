#!/usr/bin/env bash
# Лицо робота на экране: зависимости, права, служба. Запускать от wheeltec.
# Идемпотентно — можно гонять повторно.
#
# Имена переменных — латиницей. Bash допускает в именах только [A-Za-z_0-9],
# и кириллическое «имя=...» он читает как команду, которой нет.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> лицо робота: ставлю зависимости"
# Шрифт — DejaVu, у него есть кириллица; без него подпись «слушаю» превратится
# в квадратики. Он в базовом образе, но проверяем, а не предполагаем.
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
export PYGAME_HIDE_SUPPORT_PROMPT=1

# pygame — С PyPI, А НЕ ИЗ APT, и это выяснилось на живом роботе. Системный
# python3-pygame 2.1.2 собран с SDL 2.0.20, а тот на arm64 Ubuntu 22.04 при
# открытии экрана падает с «undefined symbol: _udev_device_get_action»: ищет
# символ libudev с лишним подчёркиванием в самом себе. Известная беда SDL
# 2.0.20; чинится только другим SDL. Колесо pygame с PyPI везёт свой SDL 2.28 —
# ровно тот, на котором проверки гонялись в сборщике. Поэтому мерило не
# «pygame импортируется», а «SDL не старше 2.24».
#
# Ставим в пользователя: служба идёт от /usr/bin/python3 под wheeltec, а
# пользовательские пакеты в sys.path стоят РАНЬШЕ системных — колесо перекроет
# сломанный системный pygame, не трогая его.
sdl_ok() {
  python3 - <<'PYEOF'
import sys
try:
    import pygame
except ImportError:
    sys.exit(1)
maj, mi, _ = pygame.get_sdl_version()
print(f"    pygame {pygame.version.ver}, SDL {maj}.{mi}")
sys.exit(0 if (maj, mi) >= (2, 24) else 2)
PYEOF
}

if [ ! -f "$FONT" ]; then
  sudo apt-get install -y --no-install-recommends -o Acquire::Retries=3 fonts-dejavu-core
fi

if sdl_ok; then
  echo "==> pygame с годным SDL уже стоит"
else
  echo "==> ставлю колесо pygame с PyPI (свой SDL 2.28)"
  if ! python3 -m pip --version >/dev/null 2>&1; then
    sudo apt-get install -y --no-install-recommends -o Acquire::Retries=3 python3-pip
  fi
  python3 -m pip install --user --upgrade --disable-pip-version-check pygame
  if ! sdl_ok; then
    echo "!! pygame с SDL >= 2.24 так и не встал — лицо экран не откроет."
    echo "   Проверить руками: python3 -c 'import pygame; print(pygame.get_sdl_version())'"
    exit 1
  fi
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
