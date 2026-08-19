#!/usr/bin/env bash
# Ставит определитель конца реплики: onnxruntime и модель Smart Turn.
#
# Зачем он нужен. Робот сейчас понимает, что человек договорил, по тишине:
# полтысячи миллисекунд молчания — значит всё. В тихой комнате это работает,
# а в квартире с ребёнком или телевизором тишины не наступает вовсе, и запись
# каждый раз доезжает до потолка окна. В журнале ПК это видно как «на 5.0 с
# звука» подряд: человек сказал «Кузя, сколько времени» за две секунды и ждёт
# ещё три, пока робот молча пишет комнату, уже всё расслышав.
#
# Модель слушает саму реплику — интонацию, темп, грамматику — и слышит конец
# ДО паузы. Восемь миллионов параметров, восемь мегабайт, десятки миллисекунд
# на ядро A55. Русский она поддерживает.
#
# Обрывать запись раньше прежних правил она может, позже — нет. Значит худший
# случай равен сегодняшнему поведению, и включать её не страшно.
#
#     bash scripts/setup_turn_end.sh
#
# После установки скрипт напечатает строку для ~/.robot-ai.env.

set -euo pipefail

VOICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/voice"
VENV_PY="$VOICE_DIR/.venv/bin/python"
MODEL_DIR="$HOME/smart-turn"
MODEL_FILE="$MODEL_DIR/smart-turn-v3.onnx"

# Репозиторий модели. Если авторы выпустят следующую версию, менять надо
# здесь и только здесь: имя входа, длину окна и вид ответа код читает у самой
# модели, а не хранит константами.
REPO="pipecat-ai/smart-turn-v3"
FILE_IN_REPO="smart-turn-v3.onnx"

if [ ! -x "$VENV_PY" ]; then
    echo "Нет $VENV_PY — сначала scripts/install.sh" >&2
    exit 1
fi

echo "== onnxruntime"
# Считает на CPU, видеокарты у робота нет и не будет. Колесо под aarch64 есть.
"$VENV_PY" -m pip install --quiet --upgrade onnxruntime

echo "== модель"
mkdir -p "$MODEL_DIR"
if [ -s "$MODEL_FILE" ]; then
    echo "уже есть: $MODEL_FILE"
else
    # Скачиваем напрямую, без huggingface_hub: одна библиотека ради одного
    # файла — это сотня мегабайт зависимостей на плату, где и так тесно.
    URL="https://huggingface.co/$REPO/resolve/main/$FILE_IN_REPO?download=true"
    echo "качаю $URL"
    if ! curl -fL --retry 3 --retry-delay 2 -o "$MODEL_FILE.tmp" "$URL"; then
        echo "" >&2
        echo "Не скачалось. Скачай файл руками и положи сюда:" >&2
        echo "    $MODEL_FILE" >&2
        echo "Взять здесь: https://huggingface.co/$REPO" >&2
        rm -f "$MODEL_FILE.tmp"
        exit 1
    fi
    mv "$MODEL_FILE.tmp" "$MODEL_FILE"
fi

echo "== проверка"
# Проверяем ДО того, как советовать включать: файл мог скачаться битым или
# оказаться страницей с ошибкой вместо модели, и узнать об этом лучше здесь,
# чем из молчащего робота.
"$VENV_PY" - "$MODEL_FILE" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onnxruntime as ort

путь = Path(sys.argv[1])
сессия = ort.InferenceSession(str(путь), providers=["CPUExecutionProvider"])
вход = сессия.get_inputs()[0]
выход = сессия.get_outputs()[0]
print(f"  вход:  {вход.name} {вход.shape}")
print(f"  выход: {выход.name} {выход.shape}")
размер = путь.stat().st_size / 1024 / 1024
print(f"  размер: {размер:.1f} МБ")
PY

echo
echo "Готово. Добавь в ~/.robot-ai.env:"
echo
echo "    ROBOT_TURN_MODEL=$MODEL_FILE"
echo
echo "и перезапусти:  sudo systemctl restart robot-voice"
echo
echo "В журнале появится строка «конец реплики: … отвечает …»."
echo "Если она говорит «непонятно чем», а дальше в журнале «спросили много,"
echo "оборвали 0» — добавь ещё ROBOT_TURN_OUTPUT=вероятность."
