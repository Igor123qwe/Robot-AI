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

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# --- не обновляемся на ходу ------------------------------------------------
# Голосовой сервис держит этот файл, пока робот движется — по своей команде
# или с веб-пульта. Перезапуск в этот момент убил бы поток движения; нули в
# /cmd_vel он отправить успеет, но проверять это на живом роботе не хочется.
# Таймер попробует снова через две минуты, торопиться некуда.
BUSY_FLAG="${ROBOT_BUSY_FLAG:-/run/robot-ai/moving}"
if [ "$FORCE" = 0 ] && [ -e "$BUSY_FLAG" ]; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$BUSY_FLAG") ))
  # Признак старше минуты — значит сервис убили посреди движения.
  # Дольше самой длинной поездки (три метра на 0.2 м/с — пятнадцать секунд).
  if [ "$AGE" -lt 60 ]; then
    echo "==> робот в движении, откладываю обновление"
    exit 0
  fi
  echo "==> признак движения залежался ($AGE с), не верю ему"
fi

if [ "$(id -u)" = 0 ]; then
  SUDO=""
  git() { runuser -u "$OWNER" -- env "HOME=$(getent passwd "$OWNER" | cut -d: -f6)" \
            git -C "$REPO" "$@"; }
else
  SUDO="sudo"
fi

# --- код -------------------------------------------------------------------
BEFORE="$(git rev-parse HEAD)"
if ! git fetch -q origin; then
  echo "==> нет связи с GitHub, попробую в следующий раз"
  exit 0
fi

# Робот — не рабочая машина, а потребитель кода: его ветка обязана совпадать с
# GitHub. Раньше одного локального коммита или правки файла хватало, чтобы
# --ff-only падал каждые две минуты вечно и обновления переставали приезжать
# молча. Теперь расхождение — не тупик: откладываем локальное в отдельную
# ветку (ничего не теряется) и встаём на удалённую.
if ! git merge --ff-only -q '@{u}' 2>/dev/null; then
  MARK="local-$(date +%Y%m%d-%H%M%S)"
  echo "==> локальные изменения расходятся с GitHub, откладываю их как $MARK"
  # Незакоммиченное — в stash, коммиты — в ветку. Достать: git stash list,
  # git log local-*. Ничего не удаляется, но робот встаёт на код с GitHub.
  git stash push -u -q -m "$MARK" || true
  git branch "$MARK" HEAD || true
  git reset -q --hard '@{u}'
fi
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
# Статику в web/ не трогаем: она отдаётся прямо с диска. А вот сам сервер
# (он же прокси камеры) — это код, его надо перезапускать.
# robot-base и robot-bridge живут вне этого репозитория — дёргаем их только
# если поменялся сам юнит. Иначе таймер остановит робота посреди команды.
if echo "$CHANGED" | grep -q '^voice/'; then
  RESTART="$RESTART robot-voice"
fi
if echo "$CHANGED" | grep -q '^web/server\.py$'; then
  RESTART="$RESTART robot-web"
fi

# --- перезапуск ------------------------------------------------------------
DONE=""
for unit in $RESTART; do
  case " $DONE " in *" $unit "*) continue ;; esac
  # Себя не перезапускаем: этот скрипт и есть robot-autopull, и systemctl
  # restart убьёт его на полуслове — остальные сервисы обновлены не будут.
  # Новый юнит подхватится сам при следующем срабатывании таймера.
  [ "$unit" = robot-autopull ] && continue
  DONE="$DONE $unit"
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    $SUDO systemctl restart "$unit"
    echo "    $unit — перезапущен"
  fi
done

[ -z "$DONE" ] && echo "==> перезапускать нечего"
echo "==> готово"
