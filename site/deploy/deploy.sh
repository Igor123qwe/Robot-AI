#!/usr/bin/env bash
# Ручной деплой на VPS: валидация → сборка → rsync → пинг IndexNow.
#
# Использование:
#   SSH_TARGET=user@example.ru SITE_PATH=/var/www/marketplace-site ./deploy/deploy.sh
#
# Переменные окружения сборки (домен, бренд, INDEXNOW_KEY) берутся из .env,
# если файл существует.

set -euo pipefail

cd "$(dirname "$0")/.."

: "${SSH_TARGET:?укажите SSH_TARGET, например user@example.ru}"
: "${SITE_PATH:=/var/www/marketplace-site}"

if [ -f .env ]; then
  echo "→ читаю .env"
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

echo "→ установка зависимостей"
npm ci

echo "→ валидация контента и сборка"
npm run build

echo "→ выкладка на ${SSH_TARGET}:${SITE_PATH}/dist/"
# --checksum вместо сравнения по времени: OG-картинки пересобираются
# с новым mtime, но при неизменном заголовке байт в байт совпадают
rsync -az --delete --checksum dist/ "${SSH_TARGET}:${SITE_PATH}/dist/"

echo "→ перезапуск обработчика форм"
ssh "${SSH_TARGET}" "sudo systemctl restart site-forms" || \
  echo "  предупреждение: не удалось перезапустить site-forms, проверьте вручную"

if [ -n "${INDEXNOW_KEY:-}" ]; then
  echo "→ пинг IndexNow"
  npm run indexnow
else
  echo "→ INDEXNOW_KEY не задан, пинг пропущен"
fi

echo "готово"
