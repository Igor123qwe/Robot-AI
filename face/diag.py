#!/usr/bin/env python3
"""Почему SDL говорит «kmsdrm not available» — по шагам, а не одним словом.

SDL проверяет доступность kmsdrm так: грузит libdrm и libgbm, открывает
/dev/dri/cardN, спрашивает у ядра ресурсы режима (коннекторы, энкодеры,
CRTC) и требует, чтобы всех было больше нуля. Провал любого шага он сжимает
в одно «not available», и по нему ничего не понять. Здесь те же шаги, но
каждый называется своим именем.

Запуск: python3 face/diag.py. Ничего не меняет, только читает.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import sys


class _Res(ctypes.Structure):
    # drmModeRes из libdrm: только то, что читаем.
    _fields_ = [("count_fbs", ctypes.c_int), ("fbs", ctypes.c_void_p),
                ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.c_void_p),
                ("count_connectors", ctypes.c_int), ("connectors", ctypes.c_void_p),
                ("count_encoders", ctypes.c_int), ("encoders", ctypes.c_void_p),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


def _lib(имя: str, so: str):
    путь = ctypes.util.find_library(имя)
    try:
        ctypes.CDLL(путь or so)
        print(f"  ok   {so}: грузится ({путь or so})")
        return True
    except OSError as e:
        print(f"  НЕТ  {so}: {e}")
        print(f"       поставить: sudo apt-get install --no-install-recommends "
              f"{'libgbm1' if 'gbm' in so else 'libdrm2'}")
        return False


def главный() -> int:
    print("SDL/kmsdrm: проверка по шагам")
    беда = False
    # 1. библиотеки, которые SDL грузит динамически
    if not _lib("drm", "libdrm.so.2"):
        беда = True
    if not _lib("gbm", "libgbm.so.1"):
        беда = True
    # 2. устройства
    карты = sorted(glob.glob("/dev/dri/card*"))
    if not карты:
        print("  НЕТ  /dev/dri/card* — DRM-устройства нет вовсе (панель не включена в srpi-config?)")
        return 1
    try:
        drm = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2")
    except OSError:
        return 1
    drm.drmModeGetResources.restype = ctypes.POINTER(_Res)
    drm.drmModeGetResources.argtypes = [ctypes.c_int]
    drm.drmModeFreeResources.argtypes = [ctypes.c_void_p]
    годная = None
    for карта in карты:
        try:
            fd = os.open(карта, os.O_RDWR | os.O_CLOEXEC)
        except OSError as e:
            print(f"  НЕТ  {карта}: не открывается ({e.strerror}) — "
                  f"пользователь не в группе video? (id: {os.getgid()})")
            беда = True
            continue
        try:
            рес = drm.drmModeGetResources(fd)
            if not рес:
                print(f"  НЕТ  {карта}: открылась, но drmModeGetResources → NULL "
                      f"(это не KMS-устройство или у драйвера нет modeset)")
                беда = True
                continue
            r = рес.contents
            print(f"  {'ok ' if min(r.count_connectors, r.count_encoders, r.count_crtcs) > 0 else 'НЕТ'}"
                  f"  {карта}: коннекторов {r.count_connectors}, энкодеров "
                  f"{r.count_encoders}, CRTC {r.count_crtcs}, "
                  f"до {r.max_width}×{r.max_height}")
            if min(r.count_connectors, r.count_encoders, r.count_crtcs) > 0 and годная is None:
                годная = карты.index(карта)
            drm.drmModeFreeResources(рес)
        finally:
            os.close(fd)
    if годная is None:
        print("  ИТОГ: ни одна карта не годится для KMS — SDL честно говорит «недоступно».")
        return 1
    print(f"  ИТОГ: годится {карты[годная]} → SDL_KMSDRM_DEVICE_INDEX={годная}")
    # 3. сам SDL
    os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
    os.environ["SDL_KMSDRM_DEVICE_INDEX"] = str(годная)
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    try:
        import pygame
        print(f"  pygame {pygame.version.ver}, SDL {'.'.join(map(str, pygame.get_sdl_version()))}")
        pygame.display.init()
        экран = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        print(f"  ok   экран открылся: {экран.get_width()}×{экран.get_height()} "
              f"через {pygame.display.get_driver()}")
        pygame.display.quit()
        return 0 if not беда else 1
    except Exception as e:                      # noqa: BLE001
        print(f"  НЕТ  SDL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(главный())
