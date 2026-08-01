"""Признак «робот сейчас едет» для внешних скриптов.

Автообновление с GitHub раз в две минуты перезапускает сервисы. Если это
случится посреди поездки, поток движения умрёт вместе с процессом. Нули в
/cmd_vel он отправить успеет — но полагаться на это, когда робот едет по
комнате, не хочется. Проще не обновляться на ходу.

Сервис держит файл, пока робот в движении, а scripts/update.sh на него смотрит
и откладывает перезапуск. Файл живёт в /run — это tmpfs, после перезагрузки
его гарантированно нет. systemd создаёт каталог сам (RuntimeDirectory) и
удаляет при остановке юнита, так что зависший признак не переживёт падения
сервиса.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = "/run/robot-ai/moving"


class BusyFlag:
    def __init__(self, ros, path: str = "", poll: float = 0.5) -> None:
        self.ros = ros
        self.path = Path(path or os.environ.get("ROBOT_BUSY_FLAG", DEFAULT_PATH))
        self.poll = poll
        self._stop = threading.Event()
        self._raised = False

    def start(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Не беда: без признака автообновление просто не будет знать о
            # движении. Ронять из-за этого голосового ассистента незачем.
            log.warning("не могу создать %s (%s) — признак движения отключён",
                        self.path.parent, e)
            return
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._clear()

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self._set() if self.ros.busy else self._clear()
            except OSError as e:
                log.warning("признак движения: %s", e)

    def _set(self) -> None:
        if self._raised:
            try:
                # Обновляем время: update.sh считает залежавшийся признак
                # устаревшим, чтобы убитый сервис не заблокировал обновления.
                os.utime(self.path, None)
                return
            except FileNotFoundError:
                # Файл кто-то убрал. Молча продолжать нельзя: update.sh решит,
                # что робот стоит, и перезапустит сервис прямо на ходу.
                log.warning("признак движения пропал — создаю заново")
                self._raised = False
        self.path.touch()
        self._raised = True

    def _clear(self) -> None:
        if not self._raised and not self.path.exists():
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._raised = False
