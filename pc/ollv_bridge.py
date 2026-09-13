"""Мост к Open-LLM-VTuber: наш робот говорит — их аватар это озвучивает.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ ЗАМЕНА СВОЕГО АВАТАРА. Это опциональная
надстройка: включается переменной окружения OLLV_URL, и пока она не задана,
kuzya_pc.py ведёт себя ровно как раньше — своя Live2D-страница (pc/avatar/),
свой сценарист (face/scenes.py), ни строчки в них не тронуто. Мост живёт
рядом, а не вместо.

ЧТО ТАКОЕ Open-LLM-VTuber (OLLV) И ПОЧЕМУ МОСТ, А НЕ ЗАМЕНА ВСЕГО. Это
отдельный чужой проект: свой Python-бэкенд, свой веб-фронтенд, более живая
Live2D-модель с богатым набором движений. Но своё распознавание речи,
дальномер, инструменты движения, личные дела людей у нас уже настроены и
работают — отдавать это чужому проекту незачем и рискованно. Мосту
достаётся только то, в чём чужой проект сильнее нас: как выглядит и
двигается тело персонажа, когда он озвучивает уже готовую фразу.

КАК ЭТО УСТРОЕНО (полная схема — pc/ollv_bridge/README.md):

  1. Робот просит `/tts` синтезировать фразу — тем же путём, что и раньше.
     `Мост.готово()` запоминает текст, эмоцию и ТОТ ЖЕ звук, который уже
     ушёл роботу в динамик (не синтезируем второй раз).
  2. Мост шлёт в OLLV один короткий сигнал по WebSocket — их штатный
     `ai-speak-signal`: «персонажу есть что сказать». Придуман именно для
     этого случая — не пишет в память и историю разговора, не считается
     репликой человека.
  3. OLLV зовёт СВОЙ LLM — но вместо настоящей модели там подложен провайдер-
     заглушка (`pc/ollv_bridge/robot_bridge_llm.py`, копируется в их дерево
     отдельно): она не думает, а спрашивает здесь, у Моста, что говорить
     (`GET /ollv/line`) — обычным HTTP, тем же портом, что и весь этот сервер.
  4. Их TTS-провайдер (`pc/ollv_bridge/robot_bridge_tts.py`, тоже
     подкладывается отдельно) вместо синтеза заново забирает `GET
     /ollv/audio` — тот же WAV, который уже звучит из колонки робота. Рот
     на экране идёт по настоящему звуку, без второй генерации.

ПОЧЕМУ WEBSOCKET-КЛИЕНТ СВОЙ, А НЕ ЧЕРЕЗ БИБЛИОТЕКУ. Нужно послать ровно
один короткий текстовый кадр после рукопожатия — читать ничего не надо
(fire-and-forget). Ставить ради этого стороннюю зависимость в venv,
который до сих пор обходился без неё, — не тот случай; протокол
рукопожатия и один текстовый кадр (RFC 6455) укладываются в десяток строк.
"""

from __future__ import annotations

import base64
import logging
import os
import socket
import struct
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Слово в тексте, которое OLLV вырежет и превратит в выражение модели по
# её собственному model_dict.json (обычно joy/sadness/anger/fear/surprise/
# neutral — но это имена ИЗ ФАЙЛА КОНКРЕТНОЙ МОДЕЛИ, у другой могут быть
# другие; поправь под свою модель, если имена не совпали).
ЭМОЦИЯ_В_ТЕГ = {
    "рад": "joy", "огорчён": "sadness", "встревожен": "fear",
    "не_понял": "surprise", "думаю": "neutral", "слушаю": "neutral",
    "спокоен": "neutral", "сплю": "neutral",
}

# Между попытками достучаться до OLLV, если её сейчас нет (не запущена,
# перезагружается) — не долбим чаще, чем раз в это время.
ПОВТОР = 5.0
# Таймаут на рукопожатие и отправку — их сервер не должен вешать наш.
ТАЙМАУТ = 3.0


def _рукопожатие(sock: socket.socket, host: str, path: str) -> None:
    """Один HTTP Upgrade по RFC 6455. Ответ не разбираем — только шлём."""
    ключ = base64.b64encode(os.urandom(16)).decode("ascii")
    запрос = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ключ}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    sock.sendall(запрос)
    # Читаем ответ ровно до конца заголовков — тело (если и придёт что-то
    # раньше нашей отправки) нам не нужно, мы в этот сокет ничего не читаем
    # дальше, только пишем.
    буфер = b""
    while b"\r\n\r\n" not in буфер:
        кусок = sock.recv(4096)
        if not кусок:
            raise ConnectionError("OLLV закрыл соединение на рукопожатии")
        буфер += кусок
    строка_ответа = буфер.split(b"\r\n", 1)[0]
    if b"101" not in строка_ответа:
        raise ConnectionError(f"OLLV не принял рукопожатие: {строка_ответа!r}")


def кадр_текстом(текст: str) -> bytes:
    """Один WS-кадр (RFC 6455) с текстом — маскированный, как положено клиенту.

    Чистая функция от текста — проверяется на байтах, без сокета.
    """
    данные = текст.encode("utf-8")
    маска = os.urandom(4)
    замаскировано = bytes(б ^ маска[i % 4] for i, б in enumerate(данные))
    первый_байт = 0x81                    # FIN=1, opcode=1 (текст)
    длина = len(данные)
    if длина < 126:
        заголовок = struct.pack("!BB", первый_байт, 0x80 | длина)
    elif длина < 65536:
        заголовок = struct.pack("!BBH", первый_байт, 0x80 | 126, длина)
    else:
        заголовок = struct.pack("!BBQ", первый_байт, 0x80 | 127, длина)
    return заголовок + маска + замаскировано


def _строить_wav(pcm: bytes, rate: int) -> bytes:
    """Сырые int16-сэмплы → полноценный WAV-файл с заголовком.

    Та же арифметика, что и в voice/robot_voice/tts.py._wav() — заголовок
    RIFF/WAVE, моно, 16 бит.
    """
    блок = 2
    байт_в_сек = rate * блок
    заголовок = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
        rate, байт_в_сек, блок, 16, b"data", len(pcm),
    )
    return заголовок + pcm


class Мост:
    """Очередь из одной строки: что сейчас озвучивает робот, для OLLV."""

    def __init__(self, ollv_url: str, *, часы=time.monotonic) -> None:
        разбор = urlparse(ollv_url if "://" in ollv_url else "ws://" + ollv_url)
        self.host = разбор.hostname or "127.0.0.1"
        self.port = разбор.port or 12393
        self.path = разбор.path or "/client-ws"
        self._часы = часы
        self._lock = threading.Lock()
        self._номер = 0
        self._текст = ""
        self._pcm = b""
        self._rate = 22050
        self._сигнал = threading.Event()
        self._стоп = threading.Event()

    # --- со стороны /tts: новая фраза готова ---------------------------------
    def готово(self, text: str, эмоция: str, pcm: bytes, rate: int) -> None:
        """Робот вот-вот произнесёт text — запомнить для OLLV и разбудить поток."""
        тег = ЭМОЦИЯ_В_ТЕГ.get(эмоция, "")
        with self._lock:
            self._номер += 1
            self._текст = f"[{тег}] {text}" if тег else text
            self._pcm, self._rate = pcm, int(rate)
        self._сигнал.set()

    # --- со стороны HTTP-моста (Handler в kuzya_pc.py) -----------------------
    def строка(self) -> dict:
        """Что сейчас должен сказать OLLV — читает robot_bridge_llm.py."""
        with self._lock:
            return {"номер": self._номер, "текст": self._текст}

    def звук(self) -> bytes:
        """Тот же WAV, что уже в динамике робота — читает robot_bridge_tts.py."""
        with self._lock:
            return _строить_wav(self._pcm, self._rate)

    # --- фоновый поток: держит соединение и шлёт сигнал ----------------------
    def start(self) -> None:
        threading.Thread(target=self._крутиться, name="мост-ollv", daemon=True).start()

    def stop(self) -> None:
        self._стоп.set()
        self._сигнал.set()

    def _крутиться(self) -> None:
        while not self._стоп.is_set():
            self._сигнал.wait()
            if self._стоп.is_set():
                break
            self._сигнал.clear()
            try:
                self._послать_сигнал()
            except Exception as e:                    # noqa: BLE001
                log.warning("мост к OLLV: не достучался (%s), попробую позже", e)
                time.sleep(ПОВТОР)

    def _послать_сигнал(self) -> None:
        """Новое короткое соединение на каждую фразу — надёжнее, чем держать
        одно долгое живым через реконнекты ради одного кадра раз в минуту."""
        with socket.create_connection((self.host, self.port), timeout=ТАЙМАУТ) as sock:
            sock.settimeout(ТАЙМАУТ)
            _рукопожатие(sock, f"{self.host}:{self.port}", self.path)
            sock.sendall(кадр_текстом('{"type": "ai-speak-signal"}'))
