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

Третье — микрофон. Компьютер может говорить с роботом сам, без телефона:

  POST /listen        ← вкладка шлёт сырой PCM 16 кГц с микрофона
  /listen/stream      → голосовой пайплайн забирает его потоком
  /listen/status      → JSON: идёт ли звук

Четвёртое — музыка. Радио это бесконечный поток, а трек из Яндекса кончается,
и следующий ставит робот: только у него есть очередь и ключ, а ссылки живут
минуты, так что отдать пульту плейлист вперёд нельзя.

  POST /speak/radio   ← адрес потока; пустое тело — выключить всё
  POST /speak/track   ← одна песня: {"адрес": …, "название": …}
  POST /speak/volume  ← громкость музыки, 0–1
  POST /music/ended   ← пульт: песня кончилась или не пошла
  /music/state        → JSON: сколько песен доиграно
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tls
import wsproxy

WEB_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("ROBOT_WEB_PORT", "8000"))
# Второй вход, по https: браузер пускает к микрофону только
# в защищённом контексте, и с телефона обычного http не хватает.
# Ноль — не поднимать вовсе.
TLS_PORT = int(os.environ.get("ROBOT_WEB_TLS_PORT", "8443"))
DEFAULT_PHONE = os.environ.get("ROBOT_PHONE_URL", "http://192.168.3.9:8080").rstrip("/")

# Сколько ждать телефон, прежде чем признать его недоступным.
CONNECT_TIMEOUT = 5

# Реплики держим в памяти: они живут секунды, писать их на флешку незачем.
CLIP_LIMIT = 12

# Сколько ждём, пока строка keepalive уйдёт подписчику. Без этого мёртвая
# вкладка числится живой около пятнадцати минут — столько TCP повторяет
# отправку, прежде чем сдаться. Десяти секунд хватает любой живой сети, а
# мёртвая отваливается сразу.
SSE_WRITE_TIMEOUT = 10.0
MAX_CLIP_BYTES = 8 * 1024 * 1024
# Кусок звука с микрофона компьютера: полсекунды при 16 кГц — 16 КБ.
MAX_MIC_CHUNK = 256 * 1024


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

        self._send({"id": clip_id, "url": f"/speak/{clip_id}.wav", "text": text},
                   listeners)
        return clip_id

    def broadcast(self, event: dict) -> None:
        """Событие без звука: «замолчи», «вот что я расслышал»."""
        with self._lock:
            listeners = list(self._listeners)
        self._send(event, listeners)

    @staticmethod
    def _send(event: dict, listeners: list) -> None:
        for q in listeners:
            # Заснувшая вкладка не должна тормозить робота — просто пропускаем.
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

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


class MicRelay:
    """Микрофон компьютера: вкладка шлёт сюда звук, робот забирает потоком.

    Нужно для отладки без телефона: наушники на голове, эха нет, поднимать
    IP Webcam не надо. Формат — тот же, что ждёт голосовой пайплайн:
    16 кГц, моно, int16, без заголовков.

    Тишина в паузах шлётся намеренно: без неё соединение робота уснёт по
    таймауту и он начнёт переподключаться каждые полминуты.
    """

    SILENCE = b"\x00" * 640          # 20 мс тишины при 16 кГц

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: set[queue.Queue] = set()
        # Минус бесконечность, а не ноль: monotonic() идёт от загрузки машины,
        # и с нулём первые секунды после включения выглядели бы как «кусок
        # только что приходил».
        self._last_chunk = float("-inf")
        # Чья сейчас очередь говорить в микрофон и кому мы уже отказали:
        # второе — только чтобы не писать одну и ту же строку в лог сто раз
        # в секунду.
        self._owner = ""
        self._refused = ""

    # Сколько ждать молчания прежней вкладки, прежде чем отдать микрофон
    # новой. Вкладку закрывают, обновляют, переоткрывают — и если держаться за
    # ушедшую, робот оглохнет до перезапуска сервера.
    HANDOVER = 3.0

    def take(self, кто: str) -> bool:
        """Занимает микрофон. False — им уже пользуется другая вкладка."""
        with self._lock:
            свежо = time.monotonic() - self._last_chunk < self.HANDOVER
            if self._owner and self._owner != кто and свежо:
                if кто != self._refused:
                    self._refused = кто
                    print(f"микрофон занят вкладкой {self._owner[:8]} — "
                          f"отказал {кто[:8]}", flush=True)
                return False
            if self._owner != кто:
                print(f"микрофон теперь у вкладки {кто[:8]}", flush=True)
                self._owner = кто
            return True

    def push(self, data: bytes) -> None:
        with self._lock:
            self._last_chunk = time.monotonic()
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass                  # робот не успевает — свежесть важнее

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._listeners.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)

    @property
    def live(self) -> bool:
        with self._lock:
            return time.monotonic() - self._last_chunk < 3.0


class TrackCount:
    """Сколько песен пульт доиграл. Единственный канал пульт → голос.

    Радио — поток, он не кончается; трек кончается всегда, и кто-то должен
    поставить следующий. Ставит его голосовой процесс: только у него есть
    очередь и ключ к Яндексу, а ссылки живут минуты, так что отдать пульту
    плейлист вперёд нельзя.

    Обратного канала от пульта к голосу нет вовсе: пульт разговаривает с этим
    сервером, сервер шлёт пульту SSE, голос ходит сюда запросами. Заводить
    ради одного события четвёртый канал дороже, чем раз в секунду спросить
    число, — поэтому здесь просто счётчик.

    Ошибка проигрывания считается так же, как конец: битая ссылка не должна
    останавливать вечер, следующий трек её переживёт.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = 0

    def done(self) -> int:
        with self._lock:
            self._done += 1
            return self._done

    @property
    def count(self) -> int:
        with self._lock:
            return self._done


SPEECH = SpeechQueue()
MIC = MicRelay()
TRACKS = TrackCount()


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
        if not self.path.startswith(("/camera", "/listen")):
            super().log_message(fmt, *args)

    # Каталог web/ — не файлопомойка: отдаём пульт, а не листинг и не исходники.
    def list_directory(self, path):
        self.path = "/pult.html"
        return SimpleHTTPRequestHandler.send_head(self)

    def send_head(self):
        """Пульт браузеру кэшировать нельзя.

        Робот подтягивает обновления с гитхаба каждые две минуты, а вкладка
        держит страницу открытой часами. Браузер честно берёт её из кэша — и
        человек видит вчерашний пульт при сегодняшнем роботе. На живом доме на
        это ушёл вечер: сервер уже умел включать радио, страница ещё нет, и в
        логах всё выглядело исправным.
        """
        путь = self.path.partition("?")[0]
        if путь.endswith((".html", "/")) or путь == "":
            self.send_response(200)
            try:
                тело = (WEB_DIR / "pult.html").read_bytes()
            except OSError:
                self.send_error(404, "нет пульта")
                return None
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(тело)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return io.BytesIO(тело)
        return SimpleHTTPRequestHandler.send_head(self)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        # Сравниваем раскодированный путь: иначе /server%2Epy проходит мимо
        # проверки, и исходник сервера уезжает любому в домашней сети.
        if urllib.parse.unquote(path).endswith(".py"):
            self.fail(404, "нет такой страницы")
            return
        # Мостик до rosbridge. Нужен из-за https: страница по https не имеет
        # права открыть ws:// — браузер молча блокирует это как смешанное
        # содержимое, и джойстик на телефоне перестал бы двигать робота.
        if path == "/ros" and wsproxy.это_вебсокет(self.headers):
            self.close_connection = True
            wsproxy.проводить(self.connection, self.headers)
            return
        if path == "/camera":
            self.proxy_stream(phone_base(query) + "/video")
        elif path == "/camera/snapshot":
            self.proxy_once(phone_base(query) + "/shot.jpg")
        elif path == "/camera/status":
            self.camera_status(phone_base(query))
        elif path == "/speak/events":
            self.speak_events(query)
        elif path == "/listen/stream":
            self.listen_stream()
        elif path == "/listen/status":
            self.send_json({"live": MIC.live})
        elif path == "/music/state":
            self.send_json({"кончилось": TRACKS.count})
        elif path.startswith("/speak/") and path.endswith(".wav"):
            self.speak_clip(path[len("/speak/"):-len(".wav")])
        else:
            super().do_GET()

    def do_POST(self):
        path = self.path.partition("?")[0]

        if path == "/listen":
            # Звук с микрофона компьютера. Приходит из браузера, то есть не с
            # локалхоста, — проверка ниже сюда не распространяется намеренно.
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_MIC_CHUNK:
                self.close_connection = True
                self.fail(400, "пустой или слишком большой кусок звука")
                return
            кусок = self.rfile.read(length)
            # Микрофон может быть только один. Две открытые вкладки шлют звук
            # в один буфер, куски перемешиваются, и распознавание выдаёт
            # «Кукузи — это что-то заразило» вместо «Кузя, включи музыку». На
            # живом доме на это ушёл вечер: в логе всё исправно, а робот
            # оглох. Кто пришёл раньше — тот и микрофон; остальным честный
            # отказ, и они перестают слушать.
            кто = self.headers.get("X-Mic-Id") or self.client_address[0]
            if not MIC.take(кто):
                self.fail(409, "микрофоном уже занята другая вкладка")
                return
            MIC.push(кусок)
            self.send_json({"ok": True})
            return

        if path == "/music/ended":
            # Пульт доиграл песню (или не смог её начать) и просит следующую.
            # Приходит из браузера, то есть не с локалхоста, — проверка ниже
            # сюда не распространяется намеренно, как и у микрофона.
            self.rfile.read(max(0, min(int(self.headers.get("Content-Length") or 0),
                                       1024)))
            self.send_json({"кончилось": TRACKS.done()})
            return

        if path not in ("/speak", "/speak/stop", "/speak/heard", "/speak/status",
                        "/speak/radio", "/speak/track", "/speak/volume"):
            self.fail(404, "нет такой ручки")
            return
        # Говорить роботом может только сам робот, не любое устройство в сети.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.close_connection = True
            self.fail(403, "только с самого робота")
            return

        length = int(self.headers.get("Content-Length") or 0)

        if path == "/speak/stop":
            # «Замолчи»: реплики уже в очереди вкладки, сам робот их оттуда
            # не достанет — просит пульт бросить очередь.
            SPEECH.broadcast({"stop": True})
            self.send_json({"ok": True})
            return

        if path == "/speak/radio":
            # Радио играет сама вкладка: у робота своего динамика пока нет, а
            # браузер тянет поток без нашего участия. Робот только называет
            # адрес — и он же велит замолчать, прислав пустое тело.
            body = self.rfile.read(max(0, min(length, 4096))).decode("utf-8", "replace")
            SPEECH.broadcast({"radio": body})
            self.send_json({"ok": True})
            return

        if path == "/speak/track":
            # Одна песня из Яндекса. От потока отличается тем, что кончается:
            # пульт обязан сказать об этом на /music/ended, иначе следующую
            # никто не поставит.
            body = self.rfile.read(max(0, min(length, 8192))).decode("utf-8", "replace")
            try:
                трек = json.loads(body)
            except ValueError:
                self.fail(400, "трек не разобрался")
                return
            SPEECH.broadcast({"трек": {"адрес": трек.get("адрес") or "",
                                       "название": трек.get("название") or ""}})
            self.send_json({"ok": True})
            return

        if path == "/speak/volume":
            # Громкость музыки, не голоса: голос робот делает тише сам, ещё
            # до синтеза, а поток играет вкладка и про робота ничего не знает.
            body = self.rfile.read(max(0, min(length, 32))).decode("utf-8", "replace")
            try:
                доля = min(1.0, max(0.0, float(body.strip() or 0.7)))
            except ValueError:
                self.fail(400, "громкость не число")
                return
            SPEECH.broadcast({"громкость": доля})
            self.send_json({"ok": True})
            return

        if path in ("/speak/heard", "/speak/status"):
            # Что робот расслышал и в каком он состоянии. Экрана у него нет, а
            # «он меня не слышит» иначе проверяется только через SSH.
            body = self.rfile.read(max(0, min(length, 4096))).decode("utf-8", "replace")
            SPEECH.broadcast({path.rsplit("/", 1)[1]: body})
            self.send_json({"ok": True})
            return

        if not 0 < length <= MAX_CLIP_BYTES:
            # Не дочитав тело, мы ломаем следующий запрос в том же соединении.
            self.close_connection = True
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
        # Обрыв без FIN — крышку ноутбука закрыли, Wi-Fi пропал, телефон
        # уснул — запись в сокет не падает: семнадцать байт keepalive спокойно
        # ложатся в буфер ядра, и ошибка приходит только когда TCP исчерпает
        # ретрансмиссии, то есть минут через пятнадцать. Всё это время робот
        # считает вкладку слушателем: синтезирует реплику, честно ждёт конца
        # звучания и не повторяет таймер, который никто не слышал.
        try:
            self.connection.settimeout(SSE_WRITE_TIMEOUT)
        except OSError:
            pass
        try:
            while True:
                try:
                    # Три секунды, а не пятнадцать: закрытую вкладку надо
                    # заметить быстро. Иначе робот считает её слушателем,
                    # честно ждёт конца реплики — и не повторяет таймер,
                    # хотя его никто не слышал.
                    event = q.get(timeout=3)
                    payload = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"   # чтобы соединение не уснуло
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            SPEECH.unsubscribe(q)
        self.close_connection = True

    # --- микрофон компьютера ---------------------------------------------
    def listen_stream(self) -> None:
        """Непрерывный PCM для голосового пайплайна. Только с самого робота."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.close_connection = True
            self.fail(403, "поток микрофона — только для самого робота")
            return

        q = MIC.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16; rate=16000; channels=1")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                try:
                    chunk = q.get(timeout=0.5)
                except queue.Empty:
                    # Никто не говорит — шлём тишину, чтобы соединение жило.
                    chunk = MicRelay.SILENCE
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            MIC.unsubscribe(q)
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


# Сколько ждём рукопожатия от клиента. Дальше — не наш человек: браузер
# здоровается сразу.
РУКОПОЖАТИЕ = 15.0


class ЗащищённыйСервер(ThreadingHTTPServer):
    """https, у которого рукопожатие делается в потоке соединения.

    Раньше защищённым делался слушающий сокет целиком, а значит рукопожатие
    случалось внутри accept() — в том же единственном потоке, который принимает
    всех. Один клиент, открывший соединение и замолчавший, замораживал https
    для всей квартиры: пульт на телефоне переставал отвечать, а робот при этом
    выглядел живым, потому что обычный http работал.
    """

    контекст: ssl.SSLContext | None = None

    def finish_request(self, request, client_address) -> None:
        try:
            request.settimeout(РУКОПОЖАТИЕ)
            защищённый = self.контекст.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError) as e:
            # Обычное дело: браузер зашёл по http на порт https, или человек
            # закрыл вкладку посреди рукопожатия. Роняем соединение, не сервер.
            log_tls(f"рукопожатие не состоялось с {client_address[0]}: {e}")
            return
        try:
            # Дальше сроков нет: и события пульта, и картинка с камеры живут
            # долго и молчат по полчаса — таймаут рвал бы их без причины.
            защищённый.settimeout(None)
            self.RequestHandlerClass(защищённый, client_address, self)
        finally:
            # Закрываем сами: обёрнутый сокет забирает себе файловый номер, и
            # уборка socketserver до него уже не дотянется.
            try:
                защищённый.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            защищённый.close()


def log_tls(строка: str) -> None:
    """Тихо, но не молча: такое случается часто и пугать им человека незачем."""
    print(f"https: {строка}", file=sys.stderr, flush=True)


def _слушать_https() -> ThreadingHTTPServer | None:
    """Второй вход, по https. Без него микрофон с телефона не работает.

    Обычный http на своём порту остаётся нетронутым: по нему с роботом
    разговаривает его же голосовая служба через 127.0.0.1, и ей сертификаты
    ни к чему.
    """
    бумаги, беда = tls.выписать()
    if бумаги is None:
        print(f"https не поднял: {беда}. Микрофон с телефона не заработает,"
              " см. README", flush=True)
        return None
    сертификат, ключ = бумаги
    контекст = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        контекст.load_cert_chain(сертификат, ключ)
    except (ssl.SSLError, OSError) as e:
        print(f"https не поднял: сертификат не читается ({e})", flush=True)
        return None
    try:
        сервер = ЗащищённыйСервер(("0.0.0.0", TLS_PORT), Handler)
    except OSError as e:
        # Порт занят — обычно это второй запуск службы. Ронять из-за этого
        # весь пульт нельзя: по http он ещё нужен, а под systemd с вечным
        # перезапуском мы бы просто падали по кругу.
        print(f"https не поднял: порт {TLS_PORT} занят ({e}). "
              f"Пульт работает по http", flush=True)
        return None
    сервер.daemon_threads = True
    сервер.контекст = контекст
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    адрес = (tls.адреса() or ["адрес робота"])[-1]
    print(f"https на порту {TLS_PORT}: https://{адрес}:{TLS_PORT}/pult.html "
          f"— микрофон с телефона работает только отсюда", flush=True)
    return сервер


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    безопасный = _слушать_https() if TLS_PORT else None
    print(f"пульт на порту {PORT}, камера с {DEFAULT_PHONE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if безопасный is not None:
            безопасный.server_close()


if __name__ == "__main__":
    sys.exit(main())
