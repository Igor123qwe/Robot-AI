#!/usr/bin/env bash
# Забрать свежий код с GitHub и перезапустить только то, что реально изменилось.
#
# Работает в двух режимах:
#   от wheeltec  — вручную, sudo спросит пароль;
#   от root      — по таймеру (robot-autopull), git при этом идёт от владельца
#                  репозитория, иначе не найдёт его ssh-ключ.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c %U "$REPO")"
cd "$REPO"

if [ "$(id -u)" = 0 ]; then
  SUDO=""
  git() { runuser -u "$OWNER" -- env "HOME=$(getent passwd "$OWNER" | cut -d: -f6)" \
            git -C "$REPO" "$@"; }
else
  SUDO="sudo"
fi

# --- код -------------------------------------------------------------------
BEFORE="$(git rev-parse HEAD)"
git fetch -q origin
git merge --ff-only -q '@{u}'
AFTER="$(git rev-parse HEAD)"

CHANGED=""
if [ "$BEFORE" != "$AFTER" ]; then
  CHANGED="$(git diff --name-only "$BEFORE" "$AFTER")"
  echo "==> обновление: $(git log --oneline -1)"
else
  echo "==> нового нет"
fi

# --- юниты systemd ---------------------------------------------------------
# Сверяем по факту, а не по коммитам: файл в /etc мог разойтись с репозиторием.
RELOAD=0
RESTART=""
for f in systemd/*.service; do
  unit="$(basename "$f" .service)"
  if ! $SUDO cmp -s "$f" "/etc/systemd/system/$unit.service"; then
    $SUDO cp "$f" "/etc/systemd/system/$unit.service"
    RELOAD=1
    RESTART="$RESTART $unit"
    echo "    юнит $unit обновлён"
  fi
done
for f in systemd/*.timer; do
  [ -e "$f" ] || continue
  unit="$(basename "$f")"
  if ! $SUDO cmp -s "$f" "/etc/systemd/system/$unit"; then
    $SUDO cp "$f" "/etc/systemd/system/$unit"
    RELOAD=1
    echo "    таймер $unit обновлён"
  fi
done
[ "$RELOAD" = 1 ] && $SUDO systemctl daemon-reload

# --- что перезапускать из-за кода ------------------------------------------
# web/ не трогаем: http.server отдаёт файлы прямо с диска.
# robot-base и robot-bridge живут вне этого репозитория — дёргаем их только
# если поменялся сам юнит. Иначе таймер остановит робота посреди команды.
if echo "$CHANGED" | grep -q '^voice/'; then
  RESTART="$RESTART robot-voice"
fi

# --- перезапуск ------------------------------------------------------------
DONE=""
for unit in $RESTART; do
  case " $DONE " in *" $unit "*) continue ;; esac
  DONE="$DONE $unit"
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    $SUDO systemctl restart "$unit"
    echo "    $unit — перезапущен"
  fi
done

[ -z "$DONE" ] && echo "==> перезапускать нечего"
echo "==> готово"
