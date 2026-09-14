#!/usr/bin/env bash
# Включает детектор людей на BPU постоянной службой — и развязывает камеру.
#
# Почему это не делается одной командой systemctl enable. Потому что детектор
# забирает USB-камеру СЕБЕ НАВСЕГДА: держать её может только один процесс. До
# сих пор её брал пульт — по требованию, отпуская через десять секунд простоя.
# С постоянным детектором она занята всегда, и пульт вместе со зрением
# ослепли бы молча.
#
# Поэтому здесь два шага, и второй важнее первого: раздача картинки через
# web_video_server и строка ROBOT_CAMERA_URL, по которой наш веб-сервер берёт
# кадры оттуда, а не из устройства.
#
#     bash scripts/setup_body.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$HOME/.robot-ai.env"
CAMERA_URL="http://127.0.0.1:8080/stream?topic=/image"

if [ ! -d /opt/tros/humble ]; then
    echo "Нет /opt/tros/humble — это не робот с TogetheROS." >&2
    exit 1
fi
if [ ! -d /opt/tros/humble/lib/mono2d_body_detection/config ]; then
    echo "Нет моделей для BPU. Сначала:  sudo apt install hobot-models-basic" >&2
    exit 1
fi

echo "==> камера: пульт будет брать картинку у детектора"
touch "$ENV_FILE"
if grep -q "^ROBOT_CAMERA_URL=" "$ENV_FILE"; then
    echo "    уже настроено: $(grep '^ROBOT_CAMERA_URL=' "$ENV_FILE")"
else
    {
        echo ""
        echo "# Откуда пульт и зрение берут картинку. USB-камеру держит"
        echo "# детектор людей (robot-body), а раздаёт web_video_server."
        echo "# Убрать эту строку можно только вместе с robot-body — иначе"
        echo "# два процесса подерутся за /dev/video0 и проиграют оба."
        echo "ROBOT_CAMERA_URL=$CAMERA_URL"
    } >> "$ENV_FILE"
    echo "    дописал ROBOT_CAMERA_URL в $ENV_FILE"
fi

echo "==> служба"
sudo cp "$REPO/systemd/robot-body.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable robot-body
sudo systemctl restart robot-body

# Оставшийся с прежних запусков nginx надо убрать, иначе пульт не поднимется.
#
# Он приходит из штатного launch детектора: тот пятым узлом поднимает
# страничку просмотра TogetheROS, а она — nginx на порту 8000. Свой launch
# его больше не запускает (scripts/mono2d_no_web.launch.py), но nginx
# ДЕМОНИЗИРУЕТСЯ: единожды запущенный, он переживает и остановку детектора, и
# перезапуск пульта, и висит, пока его не убьют.
if command -v ss >/dev/null && sudo ss -ltnp 2>/dev/null | grep -q ':8000 .*nginx'; then
    echo "==> порт 8000 держит nginx — это страничка просмотра TogetheROS"
    echo "    она осталась с прежних запусков детектора; убираю, пульт без"
    echo "    этого порта не поднимется вовсе"
    sudo pkill -f nginx || true
    sleep 1
fi

echo "==> перезапускаю пульт, чтобы он взял новый источник"
sudo systemctl restart robot-web || true

echo
echo "готово. Проверить:"
echo "    systemctl status robot-body"
echo "    journalctl -u robot-body -f     # ждём «infer time ms»"
echo "    curl -s -o /dev/null -w '%{http_code}\\n' '$CAMERA_URL'"
echo
echo "Детектор поднимает модель на BPU и открывает камеру — первые кадры"
echo "появляются секунд через десять. Пульт до этого момента будет без"
echo "картинки, это не поломка."
