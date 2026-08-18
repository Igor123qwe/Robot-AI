#!/usr/bin/env python3
"""Пробник дальномера: показать, что плата отвечает на самом деле.

Зачем он нужен. Протокол SEN0628 собран по исходникам библиотеки DFRobot, а
не снят с живой платы: формат кадра там описан, а код команды переключения на
сетку 8×8 — нет. Догадка правдоподобная, но догадка. Проверять её вслепую,
правя драйвер и перезапуская робота, — долго и вслепую же.

Пробник ничего не чинит и никуда не встраивается. Он открывает порт, шлёт
несколько вариантов запроса и печатает ответ КАК ЕСТЬ, байтами. По этому
выводу протокол уточняется за пять минут.

    python3 scripts/tof_probe.py              # найти сам, опросить
    python3 scripts/tof_probe.py --порт /dev/ttyACM0
    python3 scripts/tof_probe.py --слушать    # молча слушать, не спрашивая

Последнее — на случай, если плата сыплет данные сама, без запроса. Некоторые
прошивки DFRobot так и делают, и тогда весь разговор про команды не нужен.

БЕЗОПАСНОСТЬ. На соседнем /dev/ttyACM* живёт контроллер приводов робота.
Слать в него байты нельзя: это команды моторам. Поэтому без --порт пробник
берёт устройство ТОЛЬКО по имени платы из /dev/serial/by-id и отказывается
работать, если имя не похоже на дальномер.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "voice"))

try:
    from robot_voice import tof
except ImportError:                       # noqa: BLE001 — запускают и отдельно
    tof = None                            # type: ignore[assignment]

СКОРОСТЬ = 115200
ОПАСНЫЕ = ("wch", "usb_single_serial")    # так представляется плата приводов


def открыть(путь: str) -> int:
    import termios

    fd = os.open(путь, os.O_RDWR | os.O_NOCTTY)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    скорость = getattr(termios, f"B{СКОРОСТЬ}")
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 2
    termios.tcsetattr(fd, termios.TCSANOW, [
        0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0,
        скорость, скорость, cc])
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def собрать(cmd: int, args: bytes = b"") -> bytes:
    return bytes([0x55, (len(args) >> 8) & 0xFF, len(args) & 0xFF, cmd]) + args


def почитать(fd: int, сколько: float = 0.4) -> bytes:
    срок = time.monotonic() + сколько
    буфер = b""
    while time.monotonic() < срок:
        кусок = os.read(fd, 4096)
        if кусок:
            буфер += кусок
        else:
            time.sleep(0.005)
    return буфер


def показать(байты: bytes, подпись: str) -> None:
    if not байты:
        print(f"  {подпись}: тишина")
        return
    print(f"  {подпись}: {len(байты)} байт")
    print(f"    начало: {байты[:16].hex(' ')}")
    if len(байты) > 16:
        print(f"    хвост:  {байты[-8:].hex(' ')}")
    # Если похоже на наш кадр — сразу покажем, что вышло.
    if tof is not None:
        разбор = tof.разобрать(байты)
        if разбор and разбор[1]:
            сетка = tof.в_метры(разбор[1])
            print(f"    РАЗОБРАЛОСЬ как команда {разбор[0]}, "
                  f"{len(разбор[1])} байт данных:")
            for ряд in сетка:
                print("      " + " ".join(f"{м:5.2f}" for м in ряд))


def выбрать_порт(явный: str) -> str:
    if явный:
        return явный
    if tof is not None:
        найдено = tof.найти_устройство()
        if найдено:
            return найдено
    # Резервный поиск, если модуль робота не подключился.
    import glob
    for образец in ("*Raspberry_Pi_Pico*", "*RP2040*", "*DFRobot*"):
        нашлось = sorted(glob.glob(f"/dev/serial/by-id/usb-{образец}-if00"))
        if нашлось:
            return нашлось[0]
    return ""


def опасен(путь: str) -> bool:
    """Не приводы ли это. Слать в моторы случайные байты нельзя."""
    имя = os.path.realpath(путь).lower()
    ссылка = путь.lower()
    return any(плохо in ссылка or плохо in имя for плохо in ОПАСНЫЕ)


def главное() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--порт", default="", help="явный путь к устройству")
    р.add_argument("--слушать", action="store_true",
                   help="ничего не спрашивать, просто слушать три секунды")
    аргс = р.parse_args()

    порт = выбрать_порт(аргс.порт)
    if not порт:
        print("Дальномер не найден. Посмотри, что есть:", file=sys.stderr)
        print("    ls -l /dev/serial/by-id/", file=sys.stderr)
        print("и запусти с --порт /dev/serial/by-id/<то, что похоже на плату>",
              file=sys.stderr)
        return 1
    if опасен(порт) and not аргс.порт:
        print(f"{порт} похож на контроллер приводов — не трогаю.", file=sys.stderr)
        return 1

    print(f"порт: {порт}")
    print(f"      → {os.path.realpath(порт)}")
    try:
        fd = открыть(порт)
    except OSError as e:
        print(f"не открылся: {e}", file=sys.stderr)
        if e.errno == 13:
            print("Нет прав. Лечится: sudo usermod -aG dialout $USER "
                  "и перезаход в систему.", file=sys.stderr)
        return 1

    try:
        if аргс.слушать:
            print("\nслушаю три секунды, ничего не спрашивая...")
            показать(почитать(fd, 3.0), "само пришло")
            return 0

        # Сначала просто послушаем: вдруг плата сыплет сама.
        print("\n1. молчим полсекунды — не сыплет ли сама")
        показать(почитать(fd, 0.5), "без запроса")

        # Потом переберём команды. Номера — из исходников библиотеки, где
        # названы не все; поэтому берём диапазон и смотрим, на что отвечает.
        print("\n2. перебираю команды")
        for cmd, args, подпись in (
            (0x01, bytes([0x08]), "0x01 + 8 (настроить сетку 8×8)"),
            (0x01, bytes([0x04]), "0x01 + 4 (настроить сетку 4×4)"),
            (0x02, b"", "0x02 (все данные)"),
            (0x03, b"", "0x03"),
            (0x04, b"", "0x04"),
            (0x05, b"", "0x05"),
        ):
            os.write(fd, собрать(cmd, args))
            показать(почитать(fd, 0.3), подпись)

        # И ещё раз «все данные» — уже после настройки.
        print("\n3. ещё раз все данные, после настройки")
        os.write(fd, собрать(0x02))
        показать(почитать(fd, 0.5), "0x02 повторно")
    finally:
        os.close(fd)

    print("\nЧто дальше: пришли этот вывод целиком. Если хоть где-то написано")
    print("«РАЗОБРАЛОСЬ» и цифры похожи на расстояния в метрах — протокол")
    print("угадан и драйвер заработает как есть.")
    return 0


if __name__ == "__main__":
    sys.exit(главное())
