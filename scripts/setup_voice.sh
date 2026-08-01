#!/usr/bin/env bash
# Ставит python-окружение для голосового пайплайна и включает сервис.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOICE="$REPO/voice"
ENVFILE="$HOME/.robot-ai.env"

# Ключ может быть и не от Anthropic напрямую — через роутер он выглядит иначе,
# поэтому проверяем только что строка заполнена и это не заглушка.
KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$ENVFILE" 2>/dev/null | cut -d= -f2-)"
case "$KEY" in
  ""|"sk-ant-..."|*"..."*)
    echo "!! в $ENVFILE не задан ANTHROPIC_API_KEY"
    echo "   заполните и запустите скрипт снова: nano $ENVFILE"
    exit 1
    ;;
esac

echo "==> системные пакеты"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-dev ffmpeg portaudio19-dev

echo "==> venv"
python3 -m venv "$VOICE/.venv"
PIP="$VOICE/.venv/bin/pip"
"$PIP" install --upgrade pip
"$PIP" install -r "$VOICE/requirements.txt"

# Piper — отдельно и необязательно: колесо под aarch64 собирается не всегда,
# а без синтеза робот всё равно работает, просто молча (реплики видно
# субтитрами в пульте). Ронять из-за этого установку незачем.
echo "==> синтез речи (Piper)"
PIPER_OK=1
if ! "$PIP" install "piper-tts>=1.2"; then
  echo "!! piper-tts не собрался"
  PIPER_OK=0
fi

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

echo
if [ "$PIPER_OK" = 0 ]; then
  echo "!! Piper не встал — робот будет работать молча."
  echo "   Реплики видно субтитрами в пульте, всё остальное работает."
  echo "   Поставить позже: $VOICE/.venv/bin/pip install piper-tts"
  echo "   Или бинарником: github.com/rhasspy/piper/releases (нужен aarch64)"
  echo
fi
echo "==> готово. Лог: sudo journalctl -u robot-voice -f"
