#!/usr/bin/env bash
# Разрешить wheeltec запускать face/screenshot.py через sudo БЕЗ ПАРОЛЯ —
# и ТОЛЬКО его. Нужно, чтобы снимок можно было забирать с компьютера одной
# командой по SSH, без интерактивного ввода пароля (там некому его вводить).
#
# ПОЧЕМУ ВООБЩЕ НУЖЕН sudo — см. шапку face/screenshot.py: ядро не отдаёт
# GEM-хендл кадра рядовому процессу без CAP_SYS_ADMIN, это защита от чтения
# чужого экрана кем попало.
#
# ОБЛАСТЬ РАЗРЕШЕНИЯ УЗКАЯ: команда без перечисленных аргументов в sudoers
# разрешает её с ЛЮБЫМИ доводами, но остаётся привязанной к конкретному
# пути интерпретатора и конкретному файлу скрипта. Это не «sudo без
# пароля вообще» — это «этот один скрипт без пароля».
#
# Запускать один раз на роботе: bash scripts/setup_screenshot_sudo.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/face/screenshot.py"
RULE_FILE=/etc/sudoers.d/robot-screenshot

if [ ! -f "$SCRIPT" ]; then
  echo "!! $SCRIPT не найден — сначала git pull" >&2
  exit 1
fi

RULE="wheeltec ALL=(root) NOPASSWD: /usr/bin/python3 $SCRIPT"

echo "==> добавляю правило sudo без пароля только для screenshot.py"
echo "$RULE" | sudo tee "$RULE_FILE.tmp" >/dev/null
sudo chmod 0440 "$RULE_FILE.tmp"

# ПРОВЕРЯЕМ ДО УСТАНОВКИ, А НЕ ПОСЛЕ. Битый файл в /etc/sudoers.d/ ломает
# sudo ЦЕЛИКОМ на всей машине — не только для этой команды, а вообще для
# всех пользователей, и чинить это без работающего sudo с этого же аккаунта
# будет уже нельзя. visudo -c проверяет синтаксис, не трогая систему.
if sudo visudo -c -f "$RULE_FILE.tmp"; then
  sudo mv "$RULE_FILE.tmp" "$RULE_FILE"
  echo "==> готово: $RULE_FILE"
  echo "    Проверить: sudo -n python3 $SCRIPT"
else
  echo "!! visudo забраковал правило — НЕ устанавливаю, чтобы не сломать sudo" >&2
  sudo rm -f "$RULE_FILE.tmp"
  exit 1
fi
