"""Мостик до rosbridge через тот же адрес, что и пульт.

Нужен из-за https. Страница по https не имеет права открыть ws:// — браузер
блокирует это как смешанное содержимое, молча. То есть, включив https ради
микрофона, мы бы разом сломали езду: джойстик на телефоне перестал бы
двигать робота, и в логе бы ничего не появилось.

Поэтому пульт ходит к rosbridge не напрямую, а сюда: wss://робот:8443/ros. Мы
принимаем соединение от браузера, сами открываем обычное ws:// к rosbridge на
127.0.0.1:9090 и переливаем байты в обе стороны.

Переливать можно именно байтами, без разбора кадров, и это не небрежность.
Браузер шлёт кадры как клиент — с маской; rosbridge как раз клиентских кадров
и ждёт. Обратно rosbridge шлёт серверные кадры, без маски, — ровно такие
браузер и ждёт от нас. Роли совпадают, поэтому кадр, пришедший с одной
стороны, законен на другой без единой правки.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import selectors
import socket

log = logging.getLogger(__name__)

# Тот же rosbridge, к которому пульт ходил напрямую.
ROS_HOST = os.environ.get("ROBOT_ROS_HOST", "127.0.0.1")
ROS_PORT = int(os.environ.get("ROBOT_ROS_PORT", "9090"))

# Волшебная строка из RFC 6455: ею подписывается ответ на рукопожатие.
МАГИЯ = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

БУФЕР = 65536
ЖДЁМ = 10.0


def _подпись(ключ: str) -> str:
    смесь = hashlib.sha1((ключ + МАГИЯ).encode("ascii")).digest()
    return base64.b64encode(смесь).decode("ascii")


def это_вебсокет(headers) -> bool:
    """Просится ли клиент на повышение до вебсокета."""
    апгрейд = (headers.get("Upgrade") or "").lower()
    коннекшн = (headers.get("Connection") or "").lower()
    return апгрейд == "websocket" and "upgrade" in коннекшн


def _рукопожатие_с_ros(канал: socket.socket) -> bool:
    """Здороваемся с rosbridge как обычный браузер."""
    ключ = base64.b64encode(os.urandom(16)).decode("ascii")
    запрос = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {ROS_HOST}:{ROS_PORT}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ключ}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    канал.sendall(запрос)

    ответ = b""
    while b"\r\n\r\n" not in ответ:
        кусок = канал.recv(БУФЕР)
        if not кусок:
            return False
        ответ += кусок
        if len(ответ) > 16384:            # столько заголовков не бывает
            return False
    голова, _, хвост = ответ.partition(b"\r\n\r\n")
    if b"101" not in голова.split(b"\r\n")[0]:
        return False
    if хвост:
        # rosbridge успел заговорить в том же пакете — не теряем.
        return хвост
    return True


def проводить(браузер: socket.socket, headers) -> None:
    """Довести соединение до rosbridge и перелить байты в обе стороны.

    Возвращается, когда любая из сторон закрылась. Сокет браузера к этому
    моменту уже наш: обычная обработка запроса для него закончилась.
    """
    ключ = headers.get("Sec-WebSocket-Key")
    if not ключ:
        браузер.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        return

    ros = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ros.settimeout(ЖДЁМ)
    try:
        ros.connect((ROS_HOST, ROS_PORT))
        начало = _рукопожатие_с_ros(ros)
    except OSError as e:
        log.warning("rosbridge не отозвался: %s", e)
        # 502, а не молчание: иначе браузер отличит это от «шасси выключено»
        # только по времени ожидания, а человек — никак.
        браузер.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        ros.close()
        return
    if not начало:
        браузер.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        ros.close()
        return

    ответ = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {_подпись(ключ)}\r\n\r\n"
    ).encode("ascii")
    браузер.sendall(ответ)
    if isinstance(начало, bytes) and начало:
        браузер.sendall(начало)

    _перелить(браузер, ros)


def _перелить(один: socket.socket, другой: socket.socket) -> None:
    """Байты туда-сюда, пока кто-нибудь не закроется."""
    for канал in (один, другой):
        канал.settimeout(None)
    выбор = selectors.DefaultSelector()
    выбор.register(один, selectors.EVENT_READ, другой)
    выбор.register(другой, selectors.EVENT_READ, один)
    try:
        while True:
            for ключ, _ in выбор.select(timeout=300):
                откуда: socket.socket = ключ.fileobj        # type: ignore[assignment]
                куда: socket.socket = ключ.data
                try:
                    кусок = откуда.recv(БУФЕР)
                except OSError:
                    return
                if not кусок:
                    return
                try:
                    куда.sendall(кусок)
                except OSError:
                    return
            else:
                if not выбор.get_map():
                    return
    finally:
        выбор.close()
        for канал in (один, другой):
            try:
                канал.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            канал.close()
