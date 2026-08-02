#!/usr/bin/env bash
# Ставит python-окружение для голосового пайплайна и включает сервис.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOICE="$REPO/voice"
ENVFILE="$HOME/.robot-ai.env"

# Ключ может быть и не от Anthropic напрямую — через роутер он выглядит иначе,
# поэтому проверяем только что строка заполнена и это не заглушка.
# || true обязателен: под set -e пустой grep уронил бы скрипт молча, без
# понятного сообщения — а именно этот случай и надо объяснить человеку.
KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$ENVFILE" 2>/dev/null | cut -d= -f2- || true)"
# Мозг может стоять на домашнем ПК — тогда облако не обязательно и ключ не
# нужен. Отказывать в установке из-за его отсутствия было бы неправильно.
PC="$(grep -E '^(ROBOT_PC_URL|ROBOT_LOCAL_API_BASE)=.+' "$ENVFILE" 2>/dev/null \
      | head -1 | cut -d= -f2- || true)"
case "$KEY" in
  ""|"sk-ant-..."|*"..."*)
    if [ -n "$PC" ]; then
      echo "-- ANTHROPIC_API_KEY не задан: работаем только через ПК ($PC)."
      echo "   Выключите ПК — и поговорить будет не с кем; команды продолжат работать."
    else
      echo "!! в $ENVFILE не задан ни ANTHROPIC_API_KEY, ни ROBOT_PC_URL"
      echo "   заполните и запустите скрипт снова: nano $ENVFILE"
      exit 1
    fi
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
VOICE_NAME="$(grep -E '^ROBOT_PIPER_VOICE=' "$ENVFILE" | cut -d= -f2 || true)"
VOICE_NAME="${VOICE_NAME:-ru_RU-irina-medium}"

# ru_RU-irina-medium → .../main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx
LOCALE="${VOICE_NAME%%-*}"          # ru_RU
LANG_DIR="${LOCALE%%_*}"            # ru
REST="${VOICE_NAME#*-}"             # irina-medium
SPEAKER="${REST%-*}"                # irina
QUALITY="${REST##*-}"               # medium
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/$LANG_DIR/$LOCALE/$SPEAKER/$QUALITY"

# Качаем во временный файл и переименовываем только после успеха. Иначе
# оборванная закачка оставляет на диске обрезок, повторный запуск видит файл
# на месте и закачку пропускает — навсегда. Робот при этом молчит: piper
# падает на каждой реплике, а проверка смотрит только на существование файла.
# (--remove-on-error не годится: он появился в curl 7.83, на роботе 7.81.)
for ext in onnx onnx.json; do
  TARGET="$VOICE/models/$VOICE_NAME.$ext"
  if [ ! -s "$TARGET" ]; then
    rm -f "$TARGET"
    echo "    качаю $VOICE_NAME.$ext"
    if curl -fL "$BASE/$VOICE_NAME.$ext" -o "$TARGET.part" && [ -s "$TARGET.part" ]; then
      mv "$TARGET.part" "$TARGET"
    else
      rm -f "$TARGET.part"
      echo "    !! не скачалось — положите файл вручную в $VOICE/models/"
    fi
  fi
done

# Модель распознавания тянется из сети при первом обращении, и делает это
# уже внутри сервиса. Если сети в тот момент нет, сервис падает на старте, а
# systemd поднимает его каждые десять секунд — молча и бесконечно. Лучше
# выяснить это здесь, пока человек смотрит на экран.
echo "==> модель распознавания"
WHISPER_NAME="$(grep -E '^ROBOT_WHISPER_MODEL=' "$ENVFILE" | cut -d= -f2 || true)"
WHISPER_NAME="${WHISPER_NAME:-base}"
if ! "$VOICE/.venv/bin/python" -c "
from faster_whisper import WhisperModel
WhisperModel('$WHISPER_NAME', device='cpu', compute_type='int8')
print('    модель $WHISPER_NAME на месте')
"; then
  echo "!! модель $WHISPER_NAME не скачалась — робот не будет слышать."
  echo "   Проверьте интернет и запустите скрипт снова."
fi

# Быстрая проверка, что окружение собралось и правила на месте: без неё
# первое «он меня не понимает» начинается с гадания.
echo "==> самопроверка"
"$VOICE/.venv/bin/python" "$VOICE/selftest.py" | tail -3 || \
  echo "!! самопроверка не прошла — запустите её отдельно и посмотрите вывод"

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
