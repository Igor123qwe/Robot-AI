"""mpv для видео на самом экране робота — та же связка, что играет музыку.

Экран у робота — DRM dumb-буферы без GPU (см. drmout.py), и владеть им может
только один процесс: DRM-мастер бывает ровно один, и это разъяснено в самом
drmout.py и в systemd/robot-face.service (там из-за этого же выключен X11).
Пока mpv показывает ролик, face.py сам НЕ рисует и держит своё DRM-устройство
закрытым (см. `_играть_видео()` в face.py) — открывает его назад, когда mpv
кончится или ролик остановили голосом.

Управление — тот же приём, что у Колонки (voice/robot_voice/music.py):
управляющий сокет mpv, JSON-строки. Смена ролика на ходу — через loadfile,
без пересоздания процесса: пересоздавать (и заново отпускать/захватывать DRM)
нужно только на старте и на стопе, а не на «следующий мультик».

Ищет видео и звук — заранее готового прямого адреса нет: mpv сам находит
yt-dlp (штатный ytdl_hook, включён по умолчанию) и разбирает обычную ссылку
вида youtube.com/watch?v=... на видео- и звукопоток.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


class Проигрыватель:
    """Один запуск mpv — один ролик (и его «следующие» через loadfile)."""

    def __init__(self, устройство: str = "") -> None:
        self.устройство = (устройство or "").strip()
        self.сокет = f"/tmp/robot-face-mpv-{uuid.uuid4().hex[:8]}.sock"
        self._proc: subprocess.Popen | None = None

    @classmethod
    def есть(cls) -> bool:
        return shutil.which("mpv") is not None

    def команда(self, url: str, громкость: int) -> list[str]:
        команда = [
            "mpv", "--no-terminal", "--really-quiet", "--vo=drm",
            f"--volume={max(0, min(100, int(громкость)))}",
            f"--input-ipc-server={self.сокет}",
            # Разрешение с запасом для скромного железа без GPU-декодера:
            # 480p достаточно для мультика на панели 1280×800, а декодировать
            # программно легче. При желании можно поднять на месте.
            "--ytdl-format=bv*[height<=480]+ba/b[height<=480]/best",
        ]
        if self.устройство:
            команда.append(f"--audio-device=alsa/{self.устройство}")
        команда.append(url)
        return команда

    def запустить(self, url: str, громкость: int = 70) -> bool:
        if not self.есть():
            log.warning("видео: mpv не установлен")
            return False
        try:
            self._proc = subprocess.Popen(
                self.команда(url, громкость),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as e:
            log.warning("видео: mpv не запустился (%s)", e)
            return False
        return True

    def жив(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _приказ(self, тело: dict) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(self.сокет)
                s.sendall((json.dumps(тело) + "\n").encode())
        except OSError:
            pass          # сокет мог не подняться или mpv уже умер — не беда

    def загрузить(self, url: str) -> None:
        self._приказ({"command": ["loadfile", url, "replace"]})

    def пауза(self, стоп: bool) -> None:
        self._приказ({"command": ["set_property", "pause", bool(стоп)]})

    def громкость(self, процент: int) -> None:
        self._приказ({"command": ["set_property", "volume",
                                  max(0, min(100, int(процент)))]})

    def стоп(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            Path(self.сокет).unlink(missing_ok=True)
        except OSError:
            pass
