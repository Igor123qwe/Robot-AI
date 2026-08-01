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
"""

from __future__ import annotations

import json
import os
import sys
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


def phone_base(query: str) -> str:
    """Адрес телефона: из параметра src, иначе из окружения."""
    src = urllib.parse.parse_qs(query).get("src", [""])[0].strip()
    if not src:
        return DEFAULT_PHONE
    if not src.startswith(("http://", "https://")):
        src = "http://" + src
    if ":" not in src.split("//", 1)[1]:
        src += ":8080"
    return src.rstrip("/")


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # Логи по каждому кадру не нужны — иначе journal распухнет.
    def log_message(self, fmt, *args):
        if not self.path.startswith("/camera"):
            super().log_message(fmt, *args)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/camera":
            self.proxy_stream(phone_base(query) + "/video")
        elif path == "/camera/snapshot":
            self.proxy_once(phone_base(query) + "/shot.jpg")
        elif path == "/camera/status":
            self.camera_status(phone_base(query))
        else:
            super().do_GET()

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
