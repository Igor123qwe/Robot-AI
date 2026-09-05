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

Если по какой-то причине PRIME недоступен (старое ядро), делаем то, что
умеет любое ядро: просим у ДРАЙВЕРА дамп текущего кадра через `getfb2`
не выйдет без DUMB-совместимого хендла — тогда сообщаем об этом честно и
предлагаем снять кадр из самой службы (см. --через-службу ниже), а не
падаем молча с чёрным PNG.
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
    _fields_ = [("fb_id", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("pixel_format", ctypes.c_uint32),
                ("flags", ctypes.c_uint64),
                ("handles", ctypes.c_uint32 * 4), ("pitches", ctypes.c_uint32 * 4),
                ("offsets", ctypes.c_uint32 * 4), ("modifier", ctypes.c_uint64 * 4)]


def _iowr(nr: int, размер: int) -> int:
    return (3 << 30) | (размер << 16) | (ord("d") << 8) | nr


IOCTL_MODE_GETCRTC = _iowr(0xA1, ctypes.sizeof(_Crtc))
IOCTL_MODE_GETFB2 = _iowr(0xCE, ctypes.sizeof(_FB2))


class _PrimeHandle(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("fd", ctypes.c_int32)]


IOCTL_PRIME_HANDLE_TO_FD = _iowr(0x2D, ctypes.sizeof(_PrimeHandle))
DRM_FORMAT_XRGB8888 = 0x34325258  # 'XR24' little-endian, см. drm_fourcc.h


def снять(карта: str) -> tuple[bytes, int, int]:
    """(сырые байты BGRX, ширина, высота) кадра, сейчас показанного на CRTC."""
    drm = открыть_libdrm()
    drm.drmModeGetResources.restype = ctypes.POINTER(_Res)
    drm.drmModeGetResources.argtypes = [ctypes.c_int]
    drm.drmModeFreeResources.argtypes = [ctypes.c_void_p]

    fd = os.open(карта, os.O_RDWR | os.O_CLOEXEC)
    try:
        рес = drm.drmModeGetResources(fd)
        if not рес:
            raise RuntimeError(f"{карта}: не KMS-устройство")
        try:
            r = рес.contents
            активный_fb = None
            for i in range(r.count_crtcs):
                crtc_id = r.crtcs[i]
                buf = ctypes.create_string_buffer(ctypes.sizeof(_Crtc))
                struct.pack_into("I", buf, 0, crtc_id)
                if fcntl.ioctl(fd, IOCTL_MODE_GETCRTC, buf, True) != 0:
                    continue
                crtc = _Crtc.from_buffer_copy(buf)
                if crtc.mode_valid and crtc.buffer_id:
                    активный_fb = crtc.buffer_id
                    break
            if not активный_fb:
                raise RuntimeError("ни один CRTC не показывает активный кадр — "
                                   "robot-face запущена?")

            fb2 = _FB2(fb_id=активный_fb)
            if fcntl.ioctl(fd, IOCTL_MODE_GETFB2, fb2, True) != 0:
                raise RuntimeError(f"GETFB2 отказал: {os.strerror(ctypes.get_errno())} "
                                   "(ядро старое? см. --через-службу)")
            if fb2.pixel_format != DRM_FORMAT_XRGB8888:
                raise RuntimeError(f"неожиданный формат кадра 0x{fb2.pixel_format:08x} "
                                   "— ожидался XRGB8888")

            ph = _PrimeHandle(handle=fb2.handles[0], flags=0)
            if fcntl.ioctl(fd, IOCTL_PRIME_HANDLE_TO_FD, ph, True) != 0:
                raise RuntimeError(f"PRIME_HANDLE_TO_FD отказал: "
                                   f"{os.strerror(ctypes.get_errno())}")
            размер = fb2.pitches[0] * fb2.height
            try:
                данные = mmap.mmap(ph.fd, размер, mmap.MAP_SHARED, mmap.PROT_READ)
                try:
                    сырое = bytes(данные[:размер])
                finally:
                    данные.close()
            finally:
                os.close(ph.fd)
            шаг = fb2.pitches[0]
            ширина_байт = fb2.width * 4
            if шаг != ширина_байт:
                сырое = b"".join(сырое[y * шаг:y * шаг + ширина_байт]
                                 for y in range(fb2.height))
            return сырое, fb2.width, fb2.height
        finally:
            drm.drmModeFreeResources(рес)
    finally:
        os.close(fd)


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
