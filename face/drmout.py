"""Вывод на панель через DRM/KMS напрямую, dumb-буферами. Без SDL, без Mesa.

ЗАЧЕМ ЭТО ВООБЩЕ. На RDK X5 DRM-драйвер — VeriSilicon `vs-drm`, и у Mesa для
него нет модуля (в журнале: «MESA-LOADER: failed to open vs-drm»). SDL-овский
kmsdrm без Mesa/GBM работать не может в принципе: ему нужен gbm_create_device,
а тот — DRI-драйвер. Это не «пакета не хватает», это архитектура. /dev/fb*
на этом ядре нет, так что fbdev-пути тоже нет.

Что остаётся и что работает на ЛЮБОМ KMS-драйвере — dumb-буферы: ядро само
выделяет кусок памяти под кадр, отдаёт его нам через mmap, а мы говорим
«покажи этот буфер на этом CRTC». Ровно так рисовал на этой панели X с
драйвером modesetting, пока мы его не выключили, — то есть путь на этом
железе проверенный, только теперь без X.

Pygame при этом остаётся: он рисует в обычную Surface в памяти (для этого
экран не нужен вовсе), а мы копируем байты в буфер ядра. Два буфера, показ
через page flip — без разрывов кадра.

ЧТО ЗДЕСЬ НЕ ДЕЛАЕТСЯ. Никакого ускорения: копирование 1280×800×4 байт
тридцать раз в секунду — это 120 МБ/с через memcpy, A55 это делает не
напрягаясь. Атомарный modeset не нужен — один CRTC, один коннектор.
"""

from __future__ import annotations

import ctypes
import errno
import glob
import mmap
import os


def открыть_libdrm() -> ctypes.CDLL:
    """Загрузить libdrm без ctypes.util.find_library.

    find_library("drm") на Linux, если ему не удаётся определить soname
    напрямую, ХОДИТ ВО ВНЕШНИЕ ПРОЦЕССЫ — ldconfig, потом gcc/objdump — через
    subprocess. На минимальном образе робота их может не быть на PATH, и
    тогда find_library не возвращает None, а бросает FileNotFoundError
    (кое-где — через posix_spawn, чьё OSError даже не называет виновника:
    голое «[Errno 2] No such file or directory»). Ровно это и произошло:
    служба robot-face (систем­ный PATH через systemd) открывала библиотеку
    исправно, а тот же код из интерактивного SSH-сеанса падал этой
    непонятной ошибкой — PATH там другой.

    Soname `libdrm.so.2` на Ubuntu/Debian неизменен уже больше десяти лет,
    и dlopen ищет его сам по стандартным путям без всякого find_library.
    Поэтому грузим напрямую, а do find_library — на самый крайний случай,
    и уже под try/except, чтобы он не мог уронить нас снова.
    """
    try:
        return ctypes.CDLL("libdrm.so.2", use_errno=True)
    except OSError:
        pass
    try:
        import ctypes.util as _util   # своё имя: "import ctypes.util" здесь
                                       # переопределило бы "ctypes" как
                                       # локальное имя во всей функции и
                                       # уронило бы более раннее ctypes.CDLL
                                       # с UnboundLocalError — на этом и
                                       # споткнулись при первом прогоне.
        путь = _util.find_library("drm")
    except Exception:                           # noqa: BLE001
        путь = None
    if not путь:
        raise OSError("libdrm не найдена ни как libdrm.so.2, ни через "
                      "find_library — установлена ли libdrm2?")
    return ctypes.CDLL(путь, use_errno=True)

# --- структуры libdrm (xf86drmMode.h) ------------------------------------------

class _Res(ctypes.Structure):
    _fields_ = [("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_connectors", ctypes.c_int),
                ("connectors", ctypes.POINTER(ctypes.c_uint32)),
                ("count_encoders", ctypes.c_int),
                ("encoders", ctypes.POINTER(ctypes.c_uint32)),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


class _Mode(ctypes.Structure):
    _fields_ = [("clock", ctypes.c_uint32),
                ("hdisplay", ctypes.c_uint16), ("hsync_start", ctypes.c_uint16),
                ("hsync_end", ctypes.c_uint16), ("htotal", ctypes.c_uint16),
                ("hskew", ctypes.c_uint16),
                ("vdisplay", ctypes.c_uint16), ("vsync_start", ctypes.c_uint16),
                ("vsync_end", ctypes.c_uint16), ("vtotal", ctypes.c_uint16),
                ("vscan", ctypes.c_uint16),
                ("vrefresh", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("type", ctypes.c_uint32), ("name", ctypes.c_char * 32)]


class _Connector(ctypes.Structure):
    _fields_ = [("connector_id", ctypes.c_uint32), ("encoder_id", ctypes.c_uint32),
                ("connector_type", ctypes.c_uint32),
                ("connector_type_id", ctypes.c_uint32),
                ("connection", ctypes.c_int),
                ("mmWidth", ctypes.c_uint32), ("mmHeight", ctypes.c_uint32),
                ("subpixel", ctypes.c_int),
                ("count_modes", ctypes.c_int), ("modes", ctypes.POINTER(_Mode)),
                ("count_props", ctypes.c_int), ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
                ("count_encoders", ctypes.c_int),
                ("encoders", ctypes.POINTER(ctypes.c_uint32))]


class _Encoder(ctypes.Structure):
    _fields_ = [("encoder_id", ctypes.c_uint32), ("encoder_type", ctypes.c_uint32),
                ("crtc_id", ctypes.c_uint32), ("possible_crtcs", ctypes.c_uint32),
                ("possible_clones", ctypes.c_uint32)]


# --- ioctl dumb-буферов (drm_mode.h) ------------------------------------------

class _CreateDumb(ctypes.Structure):
    _fields_ = [("height", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("bpp", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("handle", ctypes.c_uint32), ("pitch", ctypes.c_uint32),
                ("size", ctypes.c_uint64)]


class _MapDumb(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32), ("pad", ctypes.c_uint32),
                ("offset", ctypes.c_uint64)]


class _DestroyDumb(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32)]


def _iowr(nr: int, размер: int) -> int:
    # Linux generic: dir<<30 | size<<16 | type<<8 | nr; IOWR = чтение|запись.
    return (3 << 30) | (размер << 16) | (ord("d") << 8) | nr


IOCTL_CREATE_DUMB = _iowr(0xB2, ctypes.sizeof(_CreateDumb))
IOCTL_MAP_DUMB = _iowr(0xB3, ctypes.sizeof(_MapDumb))
IOCTL_DESTROY_DUMB = _iowr(0xB4, ctypes.sizeof(_DestroyDumb))
DRM_MODE_CONNECTED = 1
# Маски пикселя XRGB8888 в памяти little-endian: байты B, G, R, X.
МАСКИ = (0x00FF0000, 0x0000FF00, 0x000000FF, 0)


class ЭкранDRM:
    """Панель как два dumb-буфера. `показать(surface)` — кадр на экран."""

    def __init__(self, карта: str | None = None) -> None:
        self._drm = открыть_libdrm()
        d = self._drm
        d.drmModeGetResources.restype = ctypes.POINTER(_Res)
        d.drmModeGetResources.argtypes = [ctypes.c_int]
        d.drmModeFreeResources.argtypes = [ctypes.c_void_p]
        d.drmModeGetConnector.restype = ctypes.POINTER(_Connector)
        d.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
        d.drmModeFreeConnector.argtypes = [ctypes.c_void_p]
        d.drmModeGetEncoder.restype = ctypes.POINTER(_Encoder)
        d.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
        d.drmModeFreeEncoder.argtypes = [ctypes.c_void_p]
        d.drmIoctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        d.drmModeAddFB.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        d.drmModeRmFB.argtypes = [ctypes.c_int, ctypes.c_uint32]
        d.drmModeSetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                                     ctypes.POINTER(_Mode)]
        d.drmModePageFlip.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_uint32, ctypes.c_void_p]

        карты = [карта] if карта else sorted(glob.glob("/dev/dri/card*"))
        if not карты:
            raise RuntimeError("нет /dev/dri/card* — DRM-устройства нет")
        ошибки = []
        for путь in карты:
            try:
                self._открыть(путь)
                return
            except (OSError, RuntimeError) as e:
                ошибки.append(f"{путь}: {e}")
        raise RuntimeError("; ".join(ошибки))

    # --- подготовка --------------------------------------------------------
    def _открыть(self, путь: str) -> None:
        d = self._drm
        self.fd = os.open(путь, os.O_RDWR | os.O_CLOEXEC)
        рес = d.drmModeGetResources(self.fd)
        if not рес:
            os.close(self.fd)
            raise RuntimeError("drmModeGetResources → NULL (не KMS-устройство)")
        try:
            r = рес.contents
            коннектор = None
            for i in range(r.count_connectors):
                к = d.drmModeGetConnector(self.fd, r.connectors[i])
                if not к:
                    continue
                if к.contents.connection == DRM_MODE_CONNECTED and к.contents.count_modes > 0:
                    коннектор = к
                    break
                d.drmModeFreeConnector(к)
            if коннектор is None:
                raise RuntimeError("нет подключённого коннектора с режимами")
            кон = коннектор.contents
            self.connector_id = кон.connector_id
            # Первый режим у DRM — предпочтительный (preferred), это родное
            # разрешение панели.
            self.mode = _Mode()
            ctypes.pointer(self.mode)[0] = кон.modes[0]
            self.width, self.height = int(self.mode.hdisplay), int(self.mode.vdisplay)
            self.mm = (int(кон.mmWidth), int(кон.mmHeight))
            # CRTC: тот, к которому коннектор уже привязан, иначе первый
            # возможный для его энкодера.
            crtc_id = 0
            enc_ids = [кон.encoder_id] if кон.encoder_id else []
            enc_ids += [кон.encoders[i] for i in range(кон.count_encoders)]
            for eid in enc_ids:
                if not eid:
                    continue
                э = d.drmModeGetEncoder(self.fd, eid)
                if not э:
                    continue
                try:
                    if э.contents.crtc_id:
                        crtc_id = э.contents.crtc_id
                        break
                    for j in range(r.count_crtcs):
                        if э.contents.possible_crtcs & (1 << j):
                            crtc_id = r.crtcs[j]
                            break
                    if crtc_id:
                        break
                finally:
                    d.drmModeFreeEncoder(э)
            d.drmModeFreeConnector(коннектор)
            if not crtc_id:
                raise RuntimeError("не нашёл CRTC для коннектора")
            self.crtc_id = crtc_id
        finally:
            d.drmModeFreeResources(рес)

        # Два буфера: один показывается, в другой рисуем.
        self._буферы = [self._dumb() for _ in range(2)]
        self._текущий = 0
        conn = ctypes.c_uint32(self.connector_id)
        if d.drmModeSetCrtc(self.fd, self.crtc_id, self._буферы[0]["fb"], 0, 0,
                            ctypes.byref(conn), 1, ctypes.byref(self.mode)) != 0:
            e = ctypes.get_errno()
            raise RuntimeError(f"drmModeSetCrtc: {os.strerror(e)} (DRM-мастер занят? "
                               f"lightdm/X ещё жив?)")

    def _dumb(self) -> dict:
        d = self._drm
        cr = _CreateDumb(height=self.height, width=self.width, bpp=32, flags=0)
        if d.drmIoctl(self.fd, IOCTL_CREATE_DUMB, ctypes.byref(cr)) != 0:
            raise RuntimeError(f"CREATE_DUMB: {os.strerror(ctypes.get_errno())}")
        fb = ctypes.c_uint32(0)
        if d.drmModeAddFB(self.fd, self.width, self.height, 24, 32, cr.pitch,
                          cr.handle, ctypes.byref(fb)) != 0:
            raise RuntimeError(f"drmModeAddFB: {os.strerror(ctypes.get_errno())}")
        mp = _MapDumb(handle=cr.handle, pad=0, offset=0)
        if d.drmIoctl(self.fd, IOCTL_MAP_DUMB, ctypes.byref(mp)) != 0:
            raise RuntimeError(f"MAP_DUMB: {os.strerror(ctypes.get_errno())}")
        память = mmap.mmap(self.fd, cr.size, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=mp.offset)
        return {"handle": cr.handle, "pitch": int(cr.pitch), "size": int(cr.size),
                "fb": int(fb.value), "map": память}

    # --- показ ---------------------------------------------------------------
    @staticmethod
    def скопировать_кадр(сырое: bytes, буфер, ширина: int, высота: int,
                         шаг_буфера: int) -> None:
        """Строки surface → строки буфера ядра. Отдельной функцией — ЧИСТОЙ,
        без ioctl и без mmap внутри, — чтобы проверять её без DRM и без
        живой панели: подать bytes и bytearray, свериться побайтно.

        Шаг буфера ядра МОЖЕТ БЫТЬ ШИРЕ ширины кадра — ядро выравнивает
        строки под свои требования, и это не редкий случай, а обычный на
        части драйверов. Копировать одним куском тогда нельзя: кадр
        расползётся по диагонали, оставаясь «почти правильным» — то есть
        именно тем видом беды, который проходит мимо взгляда.
        """
        шаг_кадра = ширина * 4
        if шаг_буфера == шаг_кадра:
            буфер[0:len(сырое)] = сырое
            return
        for y in range(высота):
            буфер[y * шаг_буфера:y * шаг_буфера + шаг_кадра] = \
                сырое[y * шаг_кадра:(y + 1) * шаг_кадра]

    def показать(self, поверхность) -> None:
        """Скопировать pygame.Surface (XRGB8888, см. МАСКИ) и перевернуть."""
        б = self._буферы[1 - self._текущий]
        self.скопировать_кадр(поверхность.get_buffer().raw, б["map"],
                              self.width, self.height, б["pitch"])
        r = self._drm.drmModePageFlip(self.fd, self.crtc_id, б["fb"], 0, None)
        if r == 0:
            self._текущий = 1 - self._текущий
        elif -r != errno.EBUSY and ctypes.get_errno() != errno.EBUSY:
            # Не занят, а именно не смог: показываем через modeset, медленно,
            # но показываем. Занят — пропускаем кадр, следующий догонит.
            conn = ctypes.c_uint32(self.connector_id)
            self._drm.drmModeSetCrtc(self.fd, self.crtc_id, б["fb"], 0, 0,
                                     ctypes.byref(conn), 1, ctypes.byref(self.mode))
            self._текущий = 1 - self._текущий

    def закрыть(self) -> None:
        for б in getattr(self, "_буферы", []):
            try:
                б["map"].close()
                self._drm.drmModeRmFB(self.fd, б["fb"])
                dd = _DestroyDumb(handle=б["handle"])
                self._drm.drmIoctl(self.fd, IOCTL_DESTROY_DUMB, ctypes.byref(dd))
            except Exception:                   # noqa: BLE001
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass
