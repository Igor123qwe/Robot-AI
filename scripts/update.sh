#!/usr/bin/env bash
# Забрать свежий код с GitHub и перезапустить сервисы.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "==> git pull"
git pull --ff-only

# Юниты могли измениться — перекладываем и перечитываем
CHANGED=0
for f in systemd/*.service; do
  unit="$(basename "$f")"
  if ! sudo cmp -s "$f" "/etc/systemd/system/$unit"; then
    sudo cp "$f" "/etc/systemd/system/$unit"
    CHANGED=1
    echo "==> обновлён $unit"
  fi
done
[ "$CHANGED" = 1 ] && sudo systemctl daemon-reload

echo "==> перезапуск"
for unit in robot-base robot-bridge robot-web robot-voice; do
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    sudo systemctl restart "$unit"
    echo "    $unit — перезапущен"
  fi
done

echo "==> готово"
