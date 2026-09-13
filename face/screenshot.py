#!/usr/bin/env python3
"""Скриншот панели без X и без остановки robot-face.

ПОЧЕМУ НЕ import/scrot/gnome-screenshot. Все они снимают через X11 или
Wayland — на этой панели нет ни того, ни другого: лицо рисует прямо в
DRM-буфер (face/drmout.py), в обход оконного сервера. Снимать нечем, кроме
как тем же способом, каким рисует само лицо.

ЧТО ДЕЛАЕТ ЭТОТ СКРИПТ. Читает КАДРОБУФЕР, который сейчас показан на CRTC —
то есть последний кадр, который face.py уже отправил на панель через
drmModePageFlip, — и сохраняет его в PNG. Службу останавливать не нужно и
нельзя: два DRM-мастера на одну карту не уживаются, а этот скрипт мастером
не становится вовсе (открывает карту, не делает SetMaster) — он просто читает
GEM-хендл активного FB через drmModeGetFB2 → PRIME-экспорт → mmap.

ЗАПУСКАТЬ ЧЕРЕЗ sudo. Ядро отдаёт GEM-хендл кадра (тот самый, без которого
PRIME-экспорт невозможен) только DRM-мастеру или процессу с CAP_SYS_ADMIN —
это защита от того, чтобы любой процесс мог читать чужой экран. Мастером
становиться нельзя (это отняло бы экран у robot-face), а sudo даёт
CAP_SYS_ADMIN без смены мастера — ровно то, что нужно, и ничего больше.

Если PRIME недоступен (старое ядро без GETFB2/PRIME_HANDLE_TO_FD) — скрипт
честно называет отказавший шаг и причину, а не падает молча с чёрным PNG.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import glob
import mmap
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drmout import открыть_libdrm  # noqa: E402


class _Res(ctypes.Structure):
    _fields_ = [("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_connectors", ctypes.c_int),
                ("connectors", ctypes.POINTER(ctypes.c_uint32)),
                ("count_encoders", ctypes.c_int),
                ("encoders", ctypes.POINTER(ctypes.c_uint32)),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


class _Crtc(ctypes.Structure):
    _fields_ = [("crtc_id", ctypes.c_uint32), ("buffer_id", ctypes.c_uint32),
                ("x", ctypes.c_uint32), ("y", ctypes.c_uint32),
                ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("mode_valid", ctypes.c_int),
                ("mode", ctypes.c_byte * 68),      # drmModeModeInfo, поля не нужны
                ("gamma_size", ctypes.c_int)]


class _FB2(ctypes.Structure):
    """struct drm_mode_fb_cmd2 ЯДРА (drm_mode.h), байт в байт.

    БЫЛА ОШИБКА: flags стояло c_uint64 вместо c_uint32 — восемь байт вместо
    четырёх. Общий размер структуры при этом СЛУЧАЙНО совпадал (104 байта
    что так, что так: недостающее выравнивание перед modifier[4] ровно
    компенсировало лишние четыре байта flags), и по РАЗМЕРУ подмену было не
    поймать. А вот смещения полей после flags уезжали на четыре байта, и
    handles/pitches/offsets читались бы с чужих байтов — молча, без единой
    ошибки, просто неверными числами.
    """
    _fields_ = [("fb_id", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("pixel_format", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("handles", ctypes.c_uint32 * 4), ("pitches", ctypes.c_uint32 * 4),
                ("offsets", ctypes.c_uint32 * 4), ("modifier", ctypes.c_uint64 * 4)]


def _iowr(nr: int, размер: int) -> int:
    return (3 << 30) | (размер << 16) | (ord("d") << 8) | nr


# GETCRTC раньше тоже дёргался сырым ioctl'ом — и тут была НАСТОЯЩАЯ причина
# «GETCRTC: FileNotFoundError [Errno 2] No such file or directory» с робота.
# `_Crtc` выше — это структура libdrm (`drmModeCrtc`, crtc_id первым полем),
# а КЕРНЕЛЬНЫЙ ioctl DRM_IOCTL_MODE_GETCRTC ждёт другую раскладку
# (`struct drm_mode_crtc`: сначала set_connectors_ptr — 8 байт, потом
# count_connectors — 4, и только затем crtc_id, со смещением 12, а не 0).
# `struct.pack_into("I", buf, 0, crtc_id)` писал crtc_id в первые четыре
# байта — то есть в младшую половину set_connectors_ptr, а поле crtc_id,
# которое кернел действительно читает, оставалось нулём. Ядро законно не
# находило CRTC с номером 0 и отвечало ENOENT — с виду «файла нет», а на
# деле «не тот адрес в структуре».
#
# Лечение — не «поправить смещения руками», а вообще не трогать кернельную
# структуру: звать `drmModeGetCrtc()` из libdrm, которая сама делает ioctl
# правильно и отдаёт указатель на СВОЙ, другой и уже верно описанный здесь
# `_Crtc`. Именно так уже сделано в drmout.py для энкодера и коннектора —
# один урок на два файла.
IOCTL_MODE_GETFB2 = _iowr(0xCE, ctypes.sizeof(_FB2))


class _PrimeHandle(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("fd", ctypes.c_int32)]


IOCTL_PRIME_HANDLE_TO_FD = _iowr(0x2D, ctypes.sizeof(_PrimeHandle))
DRM_FORMAT_XRGB8888 = 0x34325258  # 'XR24' little-endian, см. drm_fourcc.h


def _шаг(имя: str, вызов, *args):
    """Позвать stdlib-функцию и, если она бросит OSError, назвать ШАГ.

    Голое str(OSError) не всегда содержит имя файла: у вызовов по файловому
    дескриптору (mmap, os.close) его попросту нет, и получается текст вроде
    «[Errno 2] No such file or directory» без единой зацепки, что именно
    ломалось. Ровно на этом мы и стояли — гадать дальше бессмысленно, проще
    называть шаг явно.
    """
    try:
        return вызов(*args)
    except OSError as e:
        raise RuntimeError(f"{имя}: {type(e).__name__} {e}") from e


def снять(карта: str) -> tuple[bytes, int, int]:
    """(сырые байты BGRX, ширина, высота) кадра, сейчас показанного на CRTC."""
    drm = открыть_libdrm()
    drm.drmModeGetResources.restype = ctypes.POINTER(_Res)
    drm.drmModeGetResources.argtypes = [ctypes.c_int]
    drm.drmModeFreeResources.argtypes = [ctypes.c_void_p]
    drm.drmModeGetCrtc.restype = ctypes.POINTER(_Crtc)
    drm.drmModeGetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32]
    drm.drmModeFreeCrtc.argtypes = [ctypes.c_void_p]

    fd = _шаг("открыть карту", os.open, карта, os.O_RDWR | os.O_CLOEXEC)
    try:
        рес = drm.drmModeGetResources(fd)
        if not рес:
            raise RuntimeError(f"{карта}: не KMS-устройство")
        try:
            r = рес.contents
            активный_fb = None
            for i in range(r.count_crtcs):
                crtc_id = r.crtcs[i]
                p_crtc = drm.drmModeGetCrtc(fd, crtc_id)
                if not p_crtc:
                    continue
                try:
                    crtc = p_crtc.contents
                    if crtc.mode_valid and crtc.buffer_id:
                        активный_fb = crtc.buffer_id
                        break
                finally:
                    drm.drmModeFreeCrtc(p_crtc)
            if not активный_fb:
                raise RuntimeError("ни один CRTC не показывает активный кадр — "
                                   "robot-face запущена?")

            fb2 = _FB2(fb_id=активный_fb)
            итог = _шаг("GETFB2", fcntl.ioctl, fd, IOCTL_MODE_GETFB2, fb2, True)
            if итог != 0:
                raise RuntimeError(f"GETFB2 вернул {итог} без исключения "
                                   f"(errno {ctypes.get_errno()}: "
                                   f"{os.strerror(ctypes.get_errno())}) — возможно, "
                                   f"ядро старое и не умеет GETFB2/PRIME")
            if fb2.pixel_format != DRM_FORMAT_XRGB8888:
                raise RuntimeError(f"неожиданный формат кадра 0x{fb2.pixel_format:08x} "
                                   "— ожидался XRGB8888")

            # ЯДРО НАМЕРЕННО ВОЗВРАЩАЕТ handles[0]=0, если звонящий не DRM-мастер
            # и не имеет CAP_SYS_ADMIN — это защита от чтения чужого кадра кем
            # попало (drm_mode_getfb2_ioctl → drm_framebuffer_lookup, отдаёт
            # хендл только доверенному клиенту). Мастер сейчас — robot-face, а
            # мы отдельный процесс. Без хендла PRIME_HANDLE_TO_FD законно
            # ответит ENOENT — «нет такого GEM-объекта», а на первый взгляд
            # выглядит как «нет файла». Ловим здесь, а не после третьего ioctl.
            if not fb2.handles[0]:
                raise RuntimeError(
                    "ядро не отдало GEM-хендл кадра — нет прав. Нужен "
                    "CAP_SYS_ADMIN: запусти через sudo (sudo python3 "
                    "face/screenshot.py …). Мастером DRM становиться не "
                    "надо и нельзя: это отобрало бы экран у robot-face.")

            ph = _PrimeHandle(handle=fb2.handles[0], flags=0)
            итог = _шаг("PRIME_HANDLE_TO_FD", fcntl.ioctl, fd,
                       IOCTL_PRIME_HANDLE_TO_FD, ph, True)
            if итог != 0:
                raise RuntimeError(f"PRIME_HANDLE_TO_FD вернул {итог} без исключения "
                                   f"(errno {ctypes.get_errno()}: "
                                   f"{os.strerror(ctypes.get_errno())})")
            размер = fb2.pitches[0] * fb2.height
            try:
                данные = _шаг("mmap PRIME-fd", mmap.mmap, ph.fd, размер,
                             mmap.MAP_SHARED, mmap.PROT_READ)
                try:
                    сырое = bytes(данные[:размер])
                finally:
                    данные.close()
            finally:
                _шаг("закрыть PRIME-fd", os.close, ph.fd)
            шаг = fb2.pitches[0]
            ширина_байт = fb2.width * 4
            if шаг != ширина_байт:
                сырое = b"".join(сырое[y * шаг:y * шаг + ширина_байт]
                                 for y in range(fb2.height))
            return сырое, fb2.width, fb2.height
        finally:
            drm.drmModeFreeResources(рес)
    finally:
        _шаг("закрыть карту", os.close, fd)


def _png(путь: str, bgrx: bytes, ширина: int, высота: int) -> None:
    """Сохранить BGRX как PNG без внешних библиотек — zlib есть в stdlib."""
    import zlib

    rgb = bytearray(ширина * высота * 3)
    for i in range(ширина * высота):
        b, g, r = bgrx[i * 4], bgrx[i * 4 + 1], bgrx[i * 4 + 2]
        rgb[i * 3:i * 3 + 3] = bytes((r, g, b))
    строки = bytearray()
    шаг = ширина * 3
    for y in range(высота):
        строки += b"\x00" + rgb[y * шаг:(y + 1) * шаг]

    def чанк(тип: bytes, данные: bytes) -> bytes:
        return (struct.pack(">I", len(данные)) + тип + данные
                + struct.pack(">I", zlib.crc32(тип + данные)))

    ihdr = struct.pack(">IIBBBBB", ширина, высота, 8, 2, 0, 0, 0)
    with open(путь, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(чанк(b"IHDR", ihdr))
        f.write(чанк(b"IDAT", zlib.compress(bytes(строки), 6)))
        f.write(чанк(b"IEND", b""))


def главный() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("файл", nargs="?", default="",
                    help="куда сохранить PNG (по умолчанию — со временем в имени)")
    ap.add_argument("--карта", default="", help="/dev/dri/cardN; по умолчанию — первая")
    д = ap.parse_args()

    карты = [д.карта] if д.карта else sorted(glob.glob("/dev/dri/card*"))
    if not карты:
        print("нет /dev/dri/card* — панель не включена?", file=sys.stderr)
        return 1
    ошибки = []
    for карта in карты:
        try:
            bgrx, w, h = снять(карта)
            break
        except (OSError, RuntimeError) as e:
            ошибки.append(f"{карта}: {e}")
    else:
        print("не удалось снять экран:\n  " + "\n  ".join(ошибки), file=sys.stderr)
        return 1

    путь = д.файл or f"/tmp/robot-face-{time.strftime('%Y%m%d-%H%M%S')}.png"
    _png(путь, bgrx, w, h)
    print(f"сохранено: {путь} ({w}×{h})")
    return 0


if __name__ == "__main__":
    sys.exit(главный())
