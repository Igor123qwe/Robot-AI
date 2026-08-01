#!/usr/bin/env bash
# Ставит python-окружение для голосового пайплайна и включает сервис.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOICE="$REPO/voice"
ENVFILE="$HOME/.robot-ai.env"

if ! grep -q '^ANTHROPIC_API_KEY=sk-ant-' "$ENVFILE" 2>/dev/null; then
  echo "!! в $ENVFILE не задан ANTHROPIC_API_KEY"
  echo "   заполните и запустите скрипт снова: nano $ENVFILE"
  exit 1
fi

echo "==> системные пакеты"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-dev ffmpeg portaudio19-dev

echo "==> venv"
python3 -m venv "$VOICE/.venv"
"$VOICE/.venv/bin/pip" install --upgrade pip
"$VOICE/.venv/bin/pip" install -r "$VOICE/requirements.txt"

echo "==> голос Piper"
mkdir -p "$VOICE/models"
VOICE_NAME="$(grep -E '^ROBOT_PIPER_VOICE=' "$ENVFILE" | cut -d= -f2)"
VOICE_NAME="${VOICE_NAME:-ru_RU-irina-medium}"

# ru_RU-irina-medium → .../main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx
LOCALE="${VOICE_NAME%%-*}"          # ru_RU
LANG_DIR="${LOCALE%%_*}"            # ru
REST="${VOICE_NAME#*-}"             # irina-medium
SPEAKER="${REST%-*}"                # irina
QUALITY="${REST##*-}"               # medium
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/$LANG_DIR/$LOCALE/$SPEAKER/$QUALITY"

for ext in onnx onnx.json; do
  if [ ! -f "$VOICE/models/$VOICE_NAME.$ext" ]; then
    echo "    качаю $VOICE_NAME.$ext"
    curl -fL "$BASE/$VOICE_NAME.$ext" -o "$VOICE/models/$VOICE_NAME.$ext" || {
      echo "    !! не скачалось — положите файл вручную в $VOICE/models/"
    }
  fi
done

echo "==> включаю сервис"
sudo systemctl enable robot-voice
sudo systemctl restart robot-voice

echo "==> готово. Лог: journalctl -u robot-voice -f"
