#!/usr/bin/env bash
# Установка Robot-AI на робота. Запускать от пользователя wheeltec.
# Идемпотентно — можно гонять повторно.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVFILE="$HOME/.robot-ai.env"

echo "==> репозиторий: $REPO"

# --- секреты ---------------------------------------------------------------
if [ ! -f "$ENVFILE" ]; then
  cp "$REPO/.env.example" "$ENVFILE"
  chmod 600 "$ENVFILE"
  echo "==> создан $ENVFILE — заполните ключи: nano $ENVFILE"
else
  echo "==> $ENVFILE уже есть, не трогаю"
fi

# --- systemd ---------------------------------------------------------------
echo "==> ставлю юниты systemd"
for f in "$REPO"/systemd/*.service "$REPO"/systemd/*.timer; do
  [ -e "$f" ] || continue
  sudo cp "$f" "/etc/systemd/system/$(basename "$f")"
done
sudo systemctl daemon-reload

# robot-deadman включается ВМЕСТЕ с базовыми и намеренно.
#
# Драйвер шасси WHEELTEC не тормозит сам: Cmd_Vel_Callback пакует пришедший
# Twist в кадр для STM32 — и всё. Ни таймера, ни таймаута; единственное
# обнуление в деструкторе, а он при kill -9 или segfault не зовётся. То есть
# последняя посланная скорость действует, пока не придёт следующая, и робот,
# у которого посреди поездки умер голосовой сервис, продолжает ехать.
#
# Сторож — отдельный процесс: защищаемся мы как раз от смерти охраняемого.
# Он публикует только нули, то есть навредить не может ничем.
for unit in robot-base robot-bridge robot-web robot-deadman; do
  sudo systemctl enable "$unit"
  sudo systemctl restart "$unit"
done

# Остальные службы включаются отдельно — и вот про них надо говорить ПРАВДУ.
#
# Раньше здесь стояли три безусловных «установлен, но выключен». На роботе, где
# голос давно работает, это было прямой неправдой, и неправдой опасной:
# install.sh запускают ПОСЛЕ git pull, то есть ровно тогда, когда на диске
# лежит новый код. Человек читал «выключен», ничего не перезапускал и
# оставался с прежним кодом в памяти, считая, что обновился.
#
# Работающую службу перезапускаем: она затем и написана, чтобы подхватить
# новую версию. Выключенную — не трогаем и говорим, чем включить.
# Имена переменных и функций — латиницей. Bash допускает в именах только
# [A-Za-z_0-9], и кириллическое «имя=...» он читает как команду, которой нет.
report_unit() {
  local unit="$1" how="$2" what="$3"
  if systemctl is-active --quiet "$unit"; then
    echo "==> $unit работает — перезапускаю на новый код"
    sudo systemctl restart "$unit"
  elif systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    echo "==> $unit включён, но не запущен — запускаю"
    sudo systemctl start "$unit" || true
  else
    echo "==> $unit не включён${what:+ ($what)}."
    echo "    Включить: $how"
  fi
}

report_unit robot-voice "bash $REPO/scripts/setup_voice.sh" ""
# robot-body выключен намеренно. Он забирает камеру себе НАВСЕГДА, а пульт с
# этого момента обязан брать картинку у него по ROBOT_CAMERA_URL. Включить
# его, не дописав эту строку в env, — значит ослепить пульт и «что видишь»
# разом, причём молча.
report_unit robot-body "bash $REPO/scripts/setup_body.sh" "детектор людей на BPU"
report_unit robot-autopull.timer \
  "sudo systemctl enable --now robot-autopull.timer" ""

# --- отключаем вредное наследие WHEELTEC -----------------------------------
# apstart1 поднимал точку доступа на 192.168.0.100 и ломал домашнюю сеть
if [ -f /etc/init.d/apstart1.sh ]; then
  sudo chmod -x /etc/init.d/apstart1.sh || true
  sudo systemctl mask apstart1 2>/dev/null || true
  echo "==> apstart1 отключён"
fi
sudo systemctl disable isc-dhcp-server 2>/dev/null || true

# --- итог ------------------------------------------------------------------
IP="$(hostname -I | awk '{print $1}')"
echo
echo "==> готово"
echo "    пульт:  http://$IP:8000/pult.html"
echo "    статус: systemctl status robot-base robot-bridge robot-web robot-deadman"
echo
echo "    Сторож мёртвой руки поднят. Проверить, что он на связи:"
echo "        journalctl -u robot-deadman -n 5"
echo "    Должна быть строка «подключён к ws://…, сторожу /cmd_vel»."
