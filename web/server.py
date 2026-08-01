#!/usr/bin/env python3
"""Веб-сервер пульта: статика + прокси камеры телефона.

Зачем прокси. IP Webcam живёт в сети Huawei (192.168.3.x), а ПК — в сети
Ростелекома (192.168.0.x), и напрямую до телефона из браузера не достучаться:
двойной NAT. Робот же сидит в обеих сетях сразу, поэтому пусть поток идёт
через него — тогда пульт открывается откуда угодно, лишь бы был виден робот.

Заодно это то, что нужно для будущего анализа кадров: картинка всё равно
должна приходить на робота, а не мимо него.

  /                 → web/pult.html и прочая статика
  /camera           → MJPEG-поток с телефона
  /camera/snapshot  → один кадр JPEG
  /camera/status    → JSON: доступен ли телефон

Адрес телефона берётся из ROBOT_PHONE_URL, можно переопределить на лету:
  /camera?src=http://192.168.3.9:8080

Второе назначение — динамик. Своего громкоговорителя у робота пока нет, а
IP Webcam умеет только отдавать звук, но не принимать. Поэтому голос играет
браузер, в котором открыт пульт:

  POST /speak         ← голосовой пайплайн кладёт сюда WAV (только с самого робота)
  /speak/events       → SSE: пульту сообщается, что появилась новая реплика
  /speak/<id>.wav     → сама реплика
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("ROBOT_WEB_PORT", "8000"))
DEFAULT_PHONE = os.environ.get("ROBOT_PHONE_URL", "http://192.168.3.9:8080").rstrip("/")

# Сколько ждать телефон, прежде чем признать его недоступным.
CONNECT_TIMEOUT = 5

# Реплики держим в памяти: они живут секунды, писать их на флешку незачем.
CLIP_LIMIT = 12
MAX_CLIP_BYTES = 8 * 1024 * 1024


class SpeechQueue:
    """Свежие реплики робота и подписчики, которым о них сообщать."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clips: dict[str, bytes] = {}
        self._order: list[str] = []
        # Значение — включён ли у вкладки звук. Вкладка с выключенным звуком
        # видит только титры, и робот не должен считать, что его услышали.
        self._listeners: dict[queue.Queue, bool] = {}
        self._counter = 0

    def add(self, wav: bytes, text: str) -> str:
        with self._lock:
            self._counter += 1
            clip_id = f"{int(time.time())}-{self._counter}"
            self._clips[clip_id] = wav
            self._order.append(clip_id)
            while len(self._order) > CLIP_LIMIT:
                self._clips.pop(self._order.pop(0), None)
            listeners = list(self._listeners)

        event = {"id": clip_id, "url": f"/speak/{clip_id}.wav", "text": text}
        for q in listeners:
            # Заснувшая вкладка не должна тормозить робота — просто пропускаем.
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        return clip_id

    def get(self, clip_id: str) -> bytes | None:
        with self._lock:
            return self._clips.get(clip_id)

    def subscribe(self, *, sound: bool) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._listeners[q] = sound
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.pop(q, None)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._listeners)

    @property
    def hearing(self) -> int:
        """Сколько вкладок реально проиграют реплику."""
        with self._lock:
            return sum(1 for on in self._listeners.values() if on)


SPEECH = SpeechQueue()


def _is_home_address(host: str) -> bool:
    """Домашний ли это адрес. Имена не пропускаем — только литеральный IP.

    Телефон меняет адрес по DHCP, поэтому src приходится разрешать. Но без
    ограничения это готовый прокси: робот стоит сразу в двух сетях и по
    чужой просьбе сходит куда угодно, включая интернет.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Локалхост исключён отдельно: за ним сидят службы самого робота.
    return ip.is_private and not ip.is_loopback and not ip.is_link_local


def phone_base(query: str) -> str:
    """Адрес телефона: из параметра src, иначе из окружения."""
    src = urllib.parse.parse_qs(query).get("src", [""])[0].strip()
    if not src:
        return DEFAULT_PHONE
    if not src.startswith(("http://", "https://")):
        src = "http://" + src
    parsed = urllib.parse.urlsplit(src)
    if not _is_home_address(parsed.hostname or ""):
        return DEFAULT_PHONE
    return f"http://{parsed.hostname}:{parsed.port or 8080}"


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # Логи по каждому кадру не нужны — иначе journal распухнет.
    def log_message(self, fmt, *args):
        if not self.path.startswith("/camera"):
            super().log_message(fmt, *args)

    # Каталог web/ — не файлопомойка: отдаём пульт, а не листинг и не исходники.
    def list_directory(self, path):
        self.path = "/pult.html"
        return SimpleHTTPRequestHandler.send_head(self)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path.endswith(".py"):
            self.fail(404, "нет такой страницы")
            return
        if path == "/camera":
            self.proxy_stream(phone_base(query) + "/video")
        elif path == "/camera/snapshot":
            self.proxy_once(phone_base(query) + "/shot.jpg")
        elif path == "/camera/status":
            self.camera_status(phone_base(query))
        elif path == "/speak/events":
            self.speak_events(query)
        elif path.startswith("/speak/") and path.endswith(".wav"):
            self.speak_clip(path[len("/speak/"):-len(".wav")])
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.partition("?")[0] != "/speak":
            self.fail(404, "нет такой ручки")
            return
        # Говорить роботом может только сам робот, не любое устройство в сети.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.fail(403, "только с самого робота")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_CLIP_BYTES:
            self.fail(400, "пустая или слишком большая реплика")
            return

        wav = self.rfile.read(length)
        text = urllib.parse.unquote(self.headers.get("X-Robot-Text", ""))
        clip_id = SPEECH.add(wav, text)
        self.send_json({"id": clip_id, "listeners": SPEECH.hearing,
                        "tabs": SPEECH.listeners})

    # --- голос ----------------------------------------------------------
    def speak_events(self, query: str = "") -> None:
        """SSE: держим соединение и шлём пульту id новых реплик."""
        sound = urllib.parse.parse_qs(query).get("sound", ["0"])[0] == "1"
        q = SPEECH.subscribe(sound=sound)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                    payload = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"   # чтобы соединение не уснуло
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            SPEECH.unsubscribe(q)
        self.close_connection = True

    def speak_clip(self, clip_id: str) -> None:
        wav = SPEECH.get(clip_id)
        if wav is None:
            self.fail(404, "реплика уже устарела")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(wav)

    # --- камера ---------------------------------------------------------
    def proxy_stream(self, url: str) -> None:
        """Перекладывает multipart-поток телефона клиенту как есть."""
        try:
            upstream = urllib.request.urlopen(url, timeout=CONNECT_TIMEOUT)
        except (urllib.error.URLError, OSError) as e:
            self.fail(503, f"телефон недоступен: {e}")
            return

        self.send_response(200)
        # Boundary у IP Webcam свой, поэтому Content-Type копируем целиком.
        self.send_header("Content-Type",
                         upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # зритель закрыл вкладку — это норма
        finally:
            upstream.close()
        self.close_connection = True

    def proxy_once(self, url: str) -> None:
        try:
            with urllib.request.urlopen(url, timeout=CONNECT_TIMEOUT) as upstream:
                body = upstream.read()
                ctype = upstream.headers.get("Content-Type", "image/jpeg")
        except (urllib.error.URLError, OSError) as e:
            self.fail(503, f"телефон недоступен: {e}")
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def camera_status(self, base: str) -> None:
        ok, detail = True, "ok"
        try:
            urllib.request.urlopen(base + "/", timeout=CONNECT_TIMEOUT).close()
        except (urllib.error.URLError, OSError) as e:
            ok, detail = False, str(e)
        self.send_json({"source": base, "online": ok, "detail": detail})

    # --- вспомогательное ------------------------------------------------
    def send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code: int, message: str) -> None:
        self.send_json({"online": False, "detail": message}, code)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"пульт на порту {PORT}, камера с {DEFAULT_PHONE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
