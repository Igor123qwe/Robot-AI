"""Свой сертификат для пульта — чтобы микрофон работал с телефона.

Без этого пульт с телефона наполовину мёртв: браузер пускает к микрофону
только в защищённом контексте, то есть по https или с localhost. На компьютере
это обходилось ssh-туннелем, и адрес был localhost, а на телефоне туннеля нет
— кнопка «говорить» есть, нажимается и молча отказывает.

Сертификат самоподписанный: настоящий здесь взять негде, робот живёт в
домашней сети и имени в интернете у него нет. Браузер один раз спросит
«всё равно продолжить» — и запомнит.

В сертификат вписываются все адреса робота, какие есть на момент выпуска. Если
роутер выдаст новый, при следующем запуске он выпишется заново: иначе браузер
ругался бы на несовпадение адреса, а человек думал бы, что сломался пульт.
"""

from __future__ import annotations

import datetime
import socket
import subprocess
from pathlib import Path

# Рядом с настройками робота, а не в репозитории: ключ в git не место.
ДОМ = Path.home() / ".robot-ai"
СЕРТ = ДОМ / "pult-cert.pem"
КЛЮЧ = ДОМ / "pult-key.pem"

# Десять лет: перевыпуск раз в год человек забудет, а сломается это тихо —
# просто перестанет пускать к микрофону.
ЛЕТ = 10


def адреса() -> list[str]:
    """Все адреса, по которым робота могут открыть. Без внешних библиотек."""
    найдено = {"127.0.0.1"}
    try:
        for сведения in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
            найдено.add(сведения[4][0])
    except OSError:
        pass
    # getaddrinfo по имени хоста нередко возвращает только петлю. Спрашиваем
    # ядро, каким адресом оно пошло бы наружу, — сокет никуда не соединяется.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            найдено.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(найдено)


def _san(список: list[str]) -> str:
    куски = ["DNS:localhost", f"DNS:{socket.gethostname()}"]
    куски += [f"IP:{адрес}" for адрес in список]
    return "subjectAltName=" + ",".join(куски)


def _годен(нужные: list[str]) -> bool:
    """Есть ли сертификат и покрывает ли он сегодняшние адреса."""
    if not (СЕРТ.exists() and КЛЮЧ.exists()):
        return False
    try:
        готово = subprocess.run(
            ["openssl", "x509", "-in", str(СЕРТ), "-noout", "-text"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if готово.returncode != 0:
        return False
    текст = готово.stdout
    return all(f"IP Address:{адрес}" in текст for адрес in нужные)


def выписать(куда: Path | None = None) -> tuple[Path, Path] | None:
    """Сертификат и ключ. None — если openssl нет и выписать нечем."""
    global СЕРТ, КЛЮЧ
    if куда is not None:
        СЕРТ, КЛЮЧ = куда / "pult-cert.pem", куда / "pult-key.pem"
    сегодняшние = адреса()
    if _годен(сегодняшние):
        return СЕРТ, КЛЮЧ

    СЕРТ.parent.mkdir(parents=True, exist_ok=True)
    приказ = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(КЛЮЧ), "-out", str(СЕРТ),
        "-days", str(365 * ЛЕТ),
        "-subj", "/CN=Кузя, домашний робот",
        "-addext", _san(сегодняшние),
        "-addext", "basicConstraints=critical,CA:TRUE",
    ]
    try:
        готово = subprocess.run(приказ, capture_output=True, text=True,
                                timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if готово.returncode != 0 or not СЕРТ.exists():
        return None
    # Ключ читаем только мы: он лежит в домашней папке, но папка общая.
    КЛЮЧ.chmod(0o600)
    return СЕРТ, КЛЮЧ


def годен_до() -> str:
    """Для строчки в логе. Пусто, если спросить не у кого."""
    try:
        готово = subprocess.run(
            ["openssl", "x509", "-in", str(СЕРТ), "-noout", "-enddate"],
            capture_output=True, text=True, timeout=10)
        if готово.returncode == 0:
            return готово.stdout.strip().split("=", 1)[-1]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return ""


def свежий_ли(дней: int = 30) -> bool:
    """Не пора ли перевыпускать. Отдельно от _годен: тот про адреса."""
    хвост = годен_до()
    if not хвост:
        return True
    for образец in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            конец = datetime.datetime.strptime(хвост.replace("GMT", "").strip(),
                                               образец.replace(" %Z", ""))
        except ValueError:
            continue
        return (конец - datetime.datetime.now()).days > дней
    return True
