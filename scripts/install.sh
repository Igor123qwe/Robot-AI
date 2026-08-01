#!/usr/bin/env bash
# Установка Robot-AI на робота. Запускать от пользователя wheeltec.
# Идемпотентно — можно гонять повторно.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVFILE="$HOME/.robot-ai.env"

echo "==> репозиторий: $REPO"

# --- секреты ---------------------------------------------------------------
if [ ! -f "$ENVFILE" ]; then
  cp "$REPO/.env.example" "$ENVFILE"
  chmod 600 "$ENVFILE"
  echo "==> создан $ENVFILE — заполните ключи: nano $ENVFILE"
else
  echo "==> $ENVFILE уже есть, не трогаю"
fi

# --- systemd ---------------------------------------------------------------
echo "==> ставлю юниты systemd"
for f in "$REPO"/systemd/*.service "$REPO"/systemd/*.timer; do
  [ -e "$f" ] || continue
  sudo cp "$f" "/etc/systemd/system/$(basename "$f")"
done
sudo systemctl daemon-reload

for unit in robot-base robot-bridge robot-web; do
  sudo systemctl enable "$unit"
  sudo systemctl restart "$unit"
done

# robot-voice и robot-autopull установлены, но выключены — включаются отдельно.
echo "==> robot-voice установлен, но выключен."
echo "    Включить: bash $REPO/scripts/setup_voice.sh"
echo "==> robot-autopull установлен, но выключен."
echo "    Включить: sudo systemctl enable --now robot-autopull.timer"

# --- отключаем вредное наследие WHEELTEC -----------------------------------
# apstart1 поднимал точку доступа на 192.168.0.100 и ломал домашнюю сеть
if [ -f /etc/init.d/apstart1.sh ]; then
  sudo chmod -x /etc/init.d/apstart1.sh || true
  sudo systemctl mask apstart1 2>/dev/null || true
  echo "==> apstart1 отключён"
fi
sudo systemctl disable isc-dhcp-server 2>/dev/null || true

# --- итог ------------------------------------------------------------------
IP="$(hostname -I | awk '{print $1}')"
echo
echo "==> готово"
echo "    пульт:  http://$IP:8000/pult.html"
echo "    статус: systemctl status robot-base robot-bridge robot-web"
