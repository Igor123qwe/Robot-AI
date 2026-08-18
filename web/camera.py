"""Камера, воткнутая в самого робота, — один владелец на всех.

Устройство /dev/video* захватывается монопольно: пока его держит один
процесс, второй получает отказ. А просителей у картинки двое и они разные по
характеру — пульт хочет непрерывный поток на экран, голосовой контур хочет
один кадр в тот миг, когда человек спросил «что ты видишь». Если бы каждый
открывал камеру сам, они бы отбирали её друг у друга: пульт открыт в
браузере — робот ослеп, робот посмотрел — у пульта чёрный экран.

Поэтому камерой владеет веб-сервер, и только он. Внутри крутится один ffmpeg,
последний кадр лежит в памяти, а оба просителя берут его оттуда: поток
собирается из тех же кадров, что и снимок. Голосовой службе достаётся
обычный HTTP-запрос к соседнему порту вместо борьбы за устройство.

Захват — тем же способом, что звук: отдельным процессом. Причина та же, что в
audio.py, и проверена на этом же роботе: библиотеки, которые «сами знают, как
надо», на нештатном железе отказывают молча, а ffmpeg с явными параметрами
работает и проверяется руками из консоли.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Iterator

log = logging.getLogger(__name__)

# Начало и конец кадра JPEG. Искать их в потоке безопасно: внутри сжатых
# данных байт 0xFF всегда экранируется нулём, так что случайного «конца
# кадра» в середине картинки не бывает.
НАЧАЛО = b"\xff\xd8"
КОНЕЦ = b"\xff\xd9"

# Сколько держать камеру открытой после последнего запроса. Держать вечно
# нельзя: устройство греется, ест ток и остаётся занятым для всего
# остального. Десять секунд — чтобы пульт с его потоком не передёргивал
# ffmpeg между кадрами, а разовый вопрос «что ты видишь» не оставлял камеру
# включённой до утра.
ПРОСТОЙ = 10.0

# Сколько ждать самый первый кадр. Камера просыпается не мгновенно: UVC
# договаривается о формате, а автоэкспозиция подбирает выдержку.
ПЕРВЫЙ_КАДР = 6.0


def найти_устройство() -> str:
    """Постоянное имя камеры, а не /dev/videoN.

    Номера раздаются в порядке появления при загрузке и меняются местами.
    Для звука это было бы полбеды, а здесь рядом живут номера, за которыми
    вообще не камера: у UVC-камеры их два (картинка и служебные данные), и
    второй на запрос кадра просто молчит. Ссылка в /dev/v4l/by-id
    привязана к самому устройству и переживает любые перетыкания.
    """
    свои = os.environ.get("ROBOT_CAMERA_DEVICE", "").strip()
    if свои:
        return свои
    # index0 — это как раз ветка с картинкой; вторая отдаёт метаданные.
    по_имени = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    if по_имени:
        return по_имени[0]
    видео = sorted(glob.glob("/dev/video[0-9]"))
    return видео[0] if видео else ""


class Камера:
    """Один ffmpeg на всех, последний кадр — в памяти."""

    def __init__(self, устройство: str = "", размер: str = "", частота: int = 0,
                 качество: int = 0) -> None:
        self.устройство = устройство or найти_устройство()
        self.размер = размер or os.environ.get("ROBOT_CAMERA_SIZE", "1280x1024")
        self.частота = частота or int(os.environ.get("ROBOT_CAMERA_FPS", "10"))
        self.качество = качество or int(os.environ.get("ROBOT_CAMERA_QUALITY", "6"))
        self._кадр: bytes = b""
        self._номер = 0                 # растёт с каждым кадром
        self._есть_кадр = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._поток: threading.Thread | None = None
        self._спросили = 0.0
        self._замок = threading.Lock()
        self._беда = ""

    # --- наружу ---------------------------------------------------------
    def доступна(self) -> bool:
        """Есть ли вообще что открывать. Ответ нужен до первого кадра."""
        if not self.устройство:
            return False
        if shutil.which("ffmpeg") is None:
            return False
        return os.path.exists(self.устройство)

    def кадр(self, ждать: float = ПЕРВЫЙ_КАДР) -> bytes:
        """Последний кадр. Поднимает камеру, если та спала."""
        self._спросили = time.monotonic()
        self._поднять()
        срок = time.monotonic() + ждать
        with self._есть_кадр:
            while not self._кадр and time.monotonic() < срок:
                self._есть_кадр.wait(0.2)
            return self._кадр

    def поток(self, пределом: float = 60.0) -> Iterator[bytes]:
        """Кадры по мере появления — для показа в пульте.

        Отдаёт именно НОВЫЕ кадры: без сверки по номеру быстрый потребитель
        крутился бы на одном и том же снимке и грузил бы процессор впустую.
        """
        self._поднять()
        видел = -1
        конец = time.monotonic() + пределом
        while time.monotonic() < конец:
            self._спросили = time.monotonic()
            with self._есть_кадр:
                if self._номер == видел:
                    self._есть_кадр.wait(1.0)
                if self._номер == видел or not self._кадр:
                    continue
                видел = self._номер
                кадр = self._кадр
            yield кадр

    @property
    def беда(self) -> str:
        """Почему камера не отдаёт кадры, словами ffmpeg."""
        return self._беда

    def закрыть(self) -> None:
        with self._замок:
            self._убить()

    # --- внутри ---------------------------------------------------------
    def _поднять(self) -> None:
        with self._замок:
            if self._proc is not None and self._proc.poll() is None:
                return
            if not self.доступна():
                self._беда = (f"нет устройства {self.устройство or '/dev/video*'}"
                              if shutil.which("ffmpeg") else "не установлен ffmpeg")
                return
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "v4l2",
                # Камера отдаёт готовый MJPEG, и просить его явно важно: без
                # этого ffmpeg возьмёт сырой поток, который на USB в таком
                # разрешении не помещается по полосе, и кадры посыплются.
                "-input_format", "mjpeg",
                "-video_size", self.размер,
                "-framerate", str(self.частота),
                "-i", self.устройство,
                "-q:v", str(self.качество),
                "-f", "mjpeg", "-",
            ]
            log.info("камера: открываю %s (%s, %d к/с)",
                     self.устройство, self.размер, self.частота)
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                self._беда = f"не смог запустить ffmpeg: {e}"
                log.warning("камера: %s", self._беда)
                return
            self._беда = ""
            self._поток = threading.Thread(target=self._читать, daemon=True)
            self._поток.start()

    def _убить(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("камера: ffmpeg не закрылся")
        with self._есть_кадр:
            self._кадр = b""
            self._есть_кадр.notify_all()

    def _читать(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        буфер = b""
        try:
            while proc.poll() is None:
                # read1, а не read: обычный read ждёт, пока наберётся
                # ровно столько байт, сколько попросили, и на редких или
                # мелких кадрах картинка застревала бы в трубе до заполнения
                # буфера — тем дольше, чем реже кадры. read1 отдаёт то, что
                # уже пришло.
                кусок = proc.stdout.read1(65536)
                if not кусок:
                    break
                буфер += кусок
                # Из буфера достаём ВСЕ целые кадры, а не первый попавшийся:
                # иначе на быстрой камере буфер растёт быстрее, чем мы его
                # разбираем, и картинка отстаёт от жизни на секунды.
                while True:
                    н = буфер.find(НАЧАЛО)
                    к = буфер.find(КОНЕЦ, н + 2) if н >= 0 else -1
                    if н < 0 or к < 0:
                        break
                    кадр, буфер = буфер[н:к + 2], буфер[к + 2:]
                    with self._есть_кадр:
                        self._кадр = кадр
                        self._номер += 1
                        self._есть_кадр.notify_all()
                # Камеру, которую перестали спрашивать, отпускаем.
                if time.monotonic() - self._спросили > ПРОСТОЙ:
                    log.info("камера: никто не смотрит — закрываю")
                    break
        except Exception as e:            # noqa: BLE001 — обрыв не повод падать
            log.warning("камера: чтение оборвалось (%s)", e)
        finally:
            ругань = b""
            if proc.stderr is not None:
                try:
                    ругань = proc.stderr.read() or b""
                except Exception:         # noqa: BLE001
                    ругань = b""
            if ругань:
                # Ругань ffmpeg — единственное, что отличает «занято другим
                # процессом» от «не та карта» и от «нет такого формата».
                # Прятать её значит заставить человека гадать.
                self._беда = ругань.decode("utf-8", "replace").strip().splitlines()[-1]
                log.warning("камера: %s", self._беда)
            with self._замок:
                if self._proc is proc:
                    self._proc = None
            with self._есть_кадр:
                self._кадр = b""
                self._есть_кадр.notify_all()
