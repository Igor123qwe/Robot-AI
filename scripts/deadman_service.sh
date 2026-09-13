#!/usr/bin/env bash
# Запуск сторожа мёртвой руки нужным питоном.
#
# Сторож ставится вместе с базовыми службами — раньше, чем создаётся venv
# голосового пайплайна. Прописать в юните путь к venv напрямую значило бы
# получить службу, которая на свежей установке крутится в цикле падений и
# сообщает о себе неправдой: «Restart=always», а сторожа нет.
#
# Поэтому интерпретатор ищется: сперва venv (там websocket-client точно есть),
# потом системный питон. Не нашлось ни там, ни там — говорим об этом словами
# и ждём, а не мигаем перезапусками.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCHDOG="$REPO/scripts/cmd_vel_watchdog.py"

for PY in "$REPO/voice/.venv/bin/python" /usr/bin/python3; do
    [ -x "$PY" ] || continue
    if "$PY" -c "import websocket" 2>/dev/null; then
        exec "$PY" "$WATCHDOG"
    fi
done

echo "Не нашёл питон с websocket-client — сторожить нечем." >&2
echo "Поставь голосовой пайплайн (bash scripts/setup_voice.sh) или" >&2
echo "системный пакет:  sudo apt install python3-websocket" >&2
# Спим, а не падаем: перезапуск раз в две секунды залил бы журнал и спрятал
# бы в нём всё остальное. Служба останется поднятой и починится сама, как
# только venv появится, — Restart=always её перезапустит после этого сна.
sleep 300
exit 1
