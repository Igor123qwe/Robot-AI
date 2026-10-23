#!/usr/bin/env bash
# Ставит python-окружение для голосового пайплайна и включает сервис.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOICE="$REPO/voice"
ENVFILE="$HOME/.robot-ai.env"

# Робот работает только в паре с домашним ПК: распознавание, разговор, голос и
# лицо — всё там. Ни облака, ни своего распознавания, ни своего голоса у него
# нет, поэтому единственное, без чего ставить незачем, — адрес ПК.
# || true обязателен: под set -e пустой grep уронил бы скрипт молча, без
# понятного сообщения — а именно этот случай и надо объяснить человеку.
PC="$(grep -E '^(ROBOT_PC_URL|ROBOT_LOCAL_API_BASE)=.+' "$ENVFILE" 2>/dev/null \
      | head -1 | cut -d= -f2- || true)"
if [ -z "$PC" ]; then
  echo "!! в $ENVFILE не задан ROBOT_PC_URL — без ПК робот не слышит и молчит"
  echo "   впишите адрес ПК (см. pc/README.md) и запустите скрипт снова: nano $ENVFILE"
  exit 1
fi
echo "-- работаем в паре с ПК ($PC)."
echo "   На ПК должны работать kuzya_pc.py и Ollama; ПК выключен — робот молчит,"
echo "   «Кузя, стоп» и кнопка СТОП работают и без него."

echo "==> системные пакеты"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-dev ffmpeg portaudio19-dev

echo "==> venv"
python3 -m venv "$VOICE/.venv"
PIP="$VOICE/.venv/bin/pip"
"$PIP" install --upgrade pip
"$PIP" install -r "$VOICE/requirements.txt"

# Ни модели распознавания, ни голоса синтеза роботу больше не качаем: своего
# Whisper и своего Piper у него нет, слышит и говорит за него ПК. Куда робот
# ходит, видно в журнале службы строкой «распознавание на ПК: …».

# Быстрая проверка, что окружение собралось и правила на месте: без неё
# первое «он меня не понимает» начинается с гадания.
echo "==> самопроверка"
"$VOICE/.venv/bin/python" "$VOICE/selftest.py" | tail -3 || \
  echo "!! самопроверка не прошла — запустите её отдельно и посмотрите вывод"

echo "==> включаю сервис"
sudo systemctl enable robot-voice
sudo systemctl restart robot-voice

echo
echo "==> готово. Лог: sudo journalctl -u robot-voice -f"
