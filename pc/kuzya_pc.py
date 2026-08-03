"""Мозг робота на домашнем ПК: распознавание речи и разговор.

Зачем это вообще. У RDK X5 нет видеокарты, и не будет: языковую модель он не
потянет, а Whisper тянет через силу — самую лёгкую версию и втрое медленнее
звука. Дома при этом стоит компьютер с видеокартой, который включён ровно
тогда, когда с роботом разговаривают. Пусть считает он.

Что здесь есть:

  POST /v1/messages   разговор. Снаружи выглядит как Anthropic, внутри —
                      Ollama. Робот из-за этого не меняется ни на строку:
                      в настройках он просто ходит по другому адресу.
  POST /stt           звук WAV → текст. Whisper на видеокарте разбирает
                      двухсекундную фразу примерно за треть секунды вместо
                      3.7 секунды на роботе, и заметно точнее.
  GET  /health        что живо: Ollama, модель, Whisper, видеокарта.

Почему не LiteLLM. Он умеет то же самое, но это полтысячи мегабайт
зависимостей и отдельное окно, которое надо не закрыть. Здесь один файл,
который держит и разговор, и распознавание, — и на Windows это разница
между «работает» и «в прошлый раз я что-то забыл запустить».

Запуск:
    python kuzya_pc.py --model qwen3:4b --whisper small

Робот на своей стороне: ROBOT_PC_URL=http://адрес-этого-ПК:4000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("кузя-пк")

OLLAMA = "http://127.0.0.1:11434"

# Сколько ждём Ollama. Генерация идёт секунды, а вот дозвон должен быть
# быстрым: если Ollama не запущена, робот должен узнать об этом сразу и уйти
# в облако, а не молчать полминуты.
OLLAMA_CONNECT = 3.0
OLLAMA_READ = 300.0

# Сколько держать модель в видеопамяти без работы. Умолчание Ollama — пять
# минут, после чего она выгружается, и следующая фраза снова ждёт загрузки
# десятки секунд. Робот столько ждать не станет: он уйдёт в облако и запомнит
# ПК как неотвечающий на минуту вперёд. -1 — не выгружать вовсе.
KEEP_ALIVE = -1


# --------------------------------------------------------------------------
# Перевод: язык Anthropic → язык Ollama
# --------------------------------------------------------------------------
def _build() -> str:
    """Отпечаток этого самого файла.

    Файл живёт на чужой машине и обновляется вручную, а по поведению «старая
    версия» и «новая, но не работает» неотличимы. Восемь символов в первой же
    строке лога снимают этот вопрос за секунду.
    """
    try:
        import hashlib
        return hashlib.sha1(
            Path(__file__).read_bytes()).hexdigest()[:8]
    except Exception:
        return "неизвестна"


def _proxy_trouble(error: Exception) -> bool:
    """Похоже ли, что виноват прокси, а не мы.

    Отличать важно: на прокси стоит один раз попробовать обойти, а на
    настоящую ошибку — нет, иначе она спрячется за повторной попыткой.
    """
    text = str(error).lower()
    return "proxy" in text or "socks" in text


def _text_of(content) -> str:
    """Текст из того, что Anthropic кладёт в поле content.

    Там бывает и голая строка, и список блоков — например у результата
    инструмента. Уронить сервер из-за формы данных нельзя: робот в этот
    момент ждёт ответа с заглушённым микрофоном.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        ).strip()
    return "" if content is None else str(content)


def to_ollama_messages(system, messages: list) -> list[dict]:
    """Переводит переписку в вид, который понимает Ollama.

    Две вещи не совпадают по форме, и обе важны. Первая: системный промпт у
    Anthropic отдельным полем, у Ollama — первым сообщением. Вторая:
    результат инструмента у Anthropic лежит внутри сообщения человека, а у
    Ollama это отдельное сообщение с ролью tool. Если сложить их как есть,
    модель решит, что человек зачем-то зачитал ей вслух служебный вывод.
    """
    out: list[dict] = []
    text = _text_of(system)
    if text:
        out.append({"role": "system", "content": text})

    # Ollama помечает результат именем инструмента, Anthropic — идентификатором
    # вызова. Связь между ними видна только по переписке, поэтому запоминаем.
    names: dict[str, str] = {}

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")

        if not isinstance(content, list):
            out.append({"role": role, "content": _text_of(content)})
            continue

        said: list[str] = []
        calls: list[dict] = []
        results: list[tuple[str, str]] = []

        for b in content:
            if not isinstance(b, dict):
                said.append(str(b))
                continue
            kind = b.get("type")
            if kind == "text":
                said.append(b.get("text", ""))
            elif kind == "tool_use":
                names[b.get("id", "")] = b.get("name", "")
                calls.append({"function": {
                    "name": b.get("name", ""),
                    "arguments": b.get("input") or {},
                }})
            elif kind == "tool_result":
                results.append((b.get("tool_use_id", ""),
                                _text_of(b.get("content"))))

        for call_id, result in results:
            out.append({
                "role": "tool",
                "tool_name": names.get(call_id, ""),
                "content": result,
            })

        joined = "\n".join(p for p in said if p).strip()
        if calls:
            out.append({"role": "assistant", "content": joined,
                        "tool_calls": calls})
        elif joined:
            out.append({"role": role, "content": joined})

    return out


def _no_think(messages: list[dict]) -> list[dict]:
    """Дописывает мягкий выключатель размышлений к системному сообщению."""
    out = list(messages)
    if out and out[0].get("role") == "system":
        out[0] = {**out[0], "content": out[0].get("content", "") + "\n/no_think"}
    else:
        out.insert(0, {"role": "system", "content": "/no_think"})
    return out


def to_ollama_tools(tools: list | None) -> list[dict]:
    """Схемы инструментов: у Anthropic плоско, у Ollama обёрнуто в function."""
    return [{
        "type": "function",
        "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        },
    } for t in (tools or [])]


# --------------------------------------------------------------------------
# Перевод обратно: поток Ollama → события Anthropic
# --------------------------------------------------------------------------
def _sse(event: str, data: dict) -> bytes:
    return (f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


class Unthink:
    """Отрезает размышления модели, даже когда она их не открыла.

    Qwen3 не пишет <think> в ответе: этот тег уже стоит в шаблоне запроса,
    поэтому генерация начинается сразу внутри размышлений, а наружу выходит
    только закрывающий </think>. Фильтр, который ищет пару тегов, такое
    пропускает целиком — на живом роботе он зачитал вслух полторы страницы
    рассуждений про то, каким должен быть ответ.

    Поэтому начало ответа придерживаем. Увидели </think> — всё, что было до
    него, выбрасываем. Не увидели за LIMIT символов — значит размышлений нет,
    отдаём как есть и дальше не держим.

    Порог небольшой намеренно: платим им один раз в начале ответа, а
    размышления Qwen3 начинаются с первого же токена, так что если их нет в
    первых четырёх сотнях символов — их нет вовсе.
    """

    LIMIT = 400
    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.holding = True

    def feed(self, chunk: str) -> str:
        if not self.holding:
            return chunk
        self.buf += chunk
        at = self.buf.find(self.CLOSE)
        if at >= 0:
            out = self.buf[at + len(self.CLOSE):]
            self.buf, self.holding = "", False
            return out.lstrip()
        if len(self.buf) > self.LIMIT:
            out = self.buf
            self.buf, self.holding = "", False
            # Открывающий тег всё-таки может прийти — тогда это обычная пара,
            # и её разберёт фильтр на стороне робота.
            return out
        return ""

    def close(self) -> str:
        """Хвост, который так и не оказался размышлением."""
        out, self.buf, self.holding = self.buf, "", False
        return out


class AnthropicStream:
    """Собирает из ответа Ollama те события, которых ждёт клиент Anthropic.

    Порядок событий жёсткий, и клиент на нарушение отвечает исключением
    посреди фразы. Текстовый блок открывается на первом же куске текста —
    заранее нельзя, потому что ответ может начаться сразу с вызова
    инструмента, и пустой текстовый блок собьёт разбор.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.index = 0
        self.text_open = False
        self.calls = 0

    def start(self):
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": f"msg_{int(time.time()*1000):x}",
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def text(self, chunk: str):
        if not chunk:
            return
        if not self.text_open:
            self.text_open = True
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "text", "text": ""},
            })
        yield _sse("content_block_delta", {
            "type": "content_block_delta", "index": self.index,
            "delta": {"type": "text_delta", "text": chunk},
        })

    def _close_text(self):
        if self.text_open:
            yield _sse("content_block_stop",
                       {"type": "content_block_stop", "index": self.index})
            self.text_open = False
            self.index += 1

    def tool_call(self, name: str, args):
        """Вызов инструмента. Ollama отдаёт его целиком, поэтому одним куском."""
        yield from self._close_text()
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        self.calls += 1
        yield _sse("content_block_start", {
            "type": "content_block_start", "index": self.index,
            "content_block": {
                "type": "tool_use",
                "id": f"toolu_{self.index}_{int(time.time()*1000):x}",
                "name": name,
                "input": {},
            },
        })
        yield _sse("content_block_delta", {
            "type": "content_block_delta", "index": self.index,
            "delta": {"type": "input_json_delta",
                      "partial_json": json.dumps(args or {}, ensure_ascii=False)},
        })
        yield _sse("content_block_stop",
                   {"type": "content_block_stop", "index": self.index})
        self.index += 1

    def finish(self, used_in: int, used_out: int, truncated: bool):
        yield from self._close_text()
        if self.calls:
            stop = "tool_use"
        elif truncated:
            stop = "max_tokens"
        else:
            stop = "end_turn"
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"input_tokens": used_in, "output_tokens": used_out},
        })
        yield _sse("message_stop", {"type": "message_stop"})


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
class Ollama:
    def __init__(self, base: str = OLLAMA, *, think: bool = False) -> None:
        self.base = base.rstrip("/")
        # Размышления вслух. Qwen3 и родня по умолчанию сначала пишут ход
        # мысли и только потом ответ. В переписке это полезно, для голоса —
        # разорительно: на живом роботе «Да, я здесь!» стоило 695 токенов
        # вывода и девяти секунд, из которых восемь ушли на текст, который
        # никто никогда не увидит — его вырезает фильтр по дороге к речи.
        self.think = think
        # Некоторые сборки параметр не знают. Узнаём об этом по первому
        # отказу и дальше не шлём.
        self._think_known = True

    def _post(self, path: str, payload: dict, *, stream: bool):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        # Дозвон коротким таймаутом не задать через urlopen — он один на всё.
        # Читать ответ можно долго, поэтому берём длинный: неотвеченный
        # дозвон до localhost и так падает мгновенно.
        return urllib.request.urlopen(req, timeout=OLLAMA_READ if stream else 10)

    def _payload(self, model: str, messages: list, tools: list,
                 max_tokens: int) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = tools
        if self._think_known:
            payload["think"] = self.think
        return payload

    def chat(self, model: str, messages: list, tools: list, max_tokens: int):
        """Поток ответов Ollama, разобранный по строкам."""
        # Соединение открываем до первой выдачи: тогда отказ можно исправить и
        # повторить, не показав наружу половину ответа.
        resp = None
        for _ in range(2):
            try:
                resp = self._post("/api/chat",
                                  self._payload(model, messages, tools, max_tokens),
                                  stream=True)
                break
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if self._think_known and "think" in body.lower():
                    log.warning("эта сборка Ollama не знает параметр think — "
                                "работаю без него (%s)", body[:200])
                    self._think_known = False
                    continue
                raise

        with resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    log.warning("Ollama прислала не JSON: %r", line[:200])

    def warm(self, model: str) -> None:
        """Загоняет модель в видеопамять, не дожидаясь первой фразы.

        Первый запрос к незагруженной модели идёт десятки секунд. Робот
        столько не ждёт: он уходит в облако и запоминает ПК как молчащий на
        минуту вперёд — то есть за холодный старт расплачивается не только
        первая фраза, но и все следующие в течение минуты. Проверено на живом
        роботе: ровно так и вышло.
        """
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "привет"}],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "think": self.think,
            "options": {"num_predict": 1},
        }
        started = time.monotonic()
        try:
            with self._post("/api/chat", payload, stream=True):
                pass
        except Exception as e:
            log.warning("прогреть модель не вышло (%s) — первая фраза будет долгой", e)
            return
        log.info("модель %s в памяти, прогрев занял %.0f с",
                 model, time.monotonic() - started)

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def models(self) -> list[str]:
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            return []


# --------------------------------------------------------------------------
# Whisper
# --------------------------------------------------------------------------
class Whisper:
    """Распознавание.

    Модель грузится не при обращении, а прогревом при старте — иначе первая
    фраза ждёт скачивания полугигабайта и прогрева видеокарты. Робот столько
    не ждёт: он распознаёт сам и запоминает ПК как молчащий на минуту вперёд.
    Проверено на живом роботе — именно так и вышло.

    Сервер при этом поднимается сразу: прогрев идёт в своём потоке, а /health
    честно показывает, загрузилась модель или ещё нет.
    """

    def __init__(self, size: str, language: str = "ru") -> None:
        self.size = size
        self.language = language
        self.device = "не загружена"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        from faster_whisper import WhisperModel

        # Сначала видеокарта: ради неё всё и затевалось. Если CUDA нет или
        # библиотеки не встали — честно отступаем на процессор. Даже он на
        # настольной машине быстрее, чем Cortex-A55 на роботе.
        for device, compute in (("cuda", "float16"), ("cpu", "int8")):
            try:
                model = WhisperModel(self.size, device=device, compute_type=compute)
            except Exception as e:
                if _proxy_trouble(e):
                    # VPN-клиент прописывает системный прокси схемы socks4,
                    # которую библиотека скачивания не понимает, и падает ещё
                    # до выбора устройства. Нам прокси не нужен: модель берётся
                    # с HuggingFace напрямую, а Ollama живёт на этой же машине.
                    log.warning("мешает системный прокси (%s) — обхожу и пробую снова", e)
                    os.environ["NO_PROXY"] = "*"
                    os.environ["no_proxy"] = "*"
                    try:
                        model = WhisperModel(self.size, device=device,
                                             compute_type=compute)
                    except Exception as again:
                        log.warning("whisper на %s не поднялся (%s)", device, again)
                        continue
                else:
                    log.warning("whisper на %s не поднялся (%s)", device, e)
                    continue
            self.device = device
            log.info("whisper: модель %s на %s", self.size, device)
            return model
        raise RuntimeError("whisper не поднялся ни на видеокарте, ни на процессоре")

    def warm(self) -> None:
        """Загрузить модель заранее. Зовётся при старте, в отдельном потоке."""
        started = time.monotonic()
        with self._lock:
            if self._model is None:
                try:
                    self._model = self._load()
                except Exception as e:
                    log.warning("распознавание не поднялось (%s)", e)
                    return
        log.info("распознавание готово, прогрев занял %.0f с",
                 time.monotonic() - started)

    def transcribe(self, wav: bytes) -> str:
        import io

        with self._lock:
            # Замок на всё распознавание, а не только на загрузку: одна
            # видеокарта, и две фразы разом её не поделят. Робот всё равно
            # говорит по одной.
            if self._model is None:
                self._model = self._load()
            started = time.monotonic()
            segments, info = self._model.transcribe(
                io.BytesIO(wav),
                language=self.language,
                beam_size=5,          # на видеокарте это ничего не стоит
                vad_filter=False,     # тишину уже отрезал робот
                condition_on_previous_text=False,
                temperature=[0.0, 0.2],
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )
            parts, scores = [], []
            for s in segments:
                if getattr(s, "no_speech_prob", 0.0) > 0.85:
                    continue
                parts.append(s.text.strip())
                # Насколько модель сама уверена в том, что услышала. Число
                # отрицательное: -0.2 — уверенно, -1.2 — выдумала. Робот по
                # нему решает, можно ли по этой фразе ехать.
                score = getattr(s, "avg_logprob", None)
                if score is not None:
                    scores.append(float(score))

        text = " ".join(p for p in parts if p).strip()
        sure = sum(scores) / len(scores) if scores else None
        spent = time.monotonic() - started
        length = getattr(info, "duration", 0.0) or 0.0
        log.info("whisper: %.2f с на %.1f с звука (×%.2f) | уверенность %s → %r",
                 spent, length, spent / length if length else 0,
                 f"{sure:.2f}" if sure is not None else "—", text)
        return text, sure


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "kuzya-pc"
    protocol_version = "HTTP/1.1"

    # --- вспомогательное ---
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def log_message(self, fmt, *args):
        # Своё логирование: стандартное пишет в stderr мимо настроек.
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- маршруты ---
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/health", "/"):
            cfg = self.server.cfg
            models = cfg.ollama.models()
            self._json(200, {
                "ollama": bool(models),
                "модель": cfg.model,
                "модель_скачана": any(m == cfg.model or m.startswith(cfg.model + ":")
                                      for m in models),
                "все_модели": models,
                "whisper": cfg.whisper.size,
                "whisper_на": cfg.whisper.device,
            })
            return
        self._json(404, {"error": "нет такого адреса"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/v1/messages") or path.endswith("/messages"):
            self._messages()
        elif path.endswith("/stt"):
            self._stt()
        else:
            self._json(404, {"error": "нет такого адреса"})

    # --- распознавание ---
    def _stt(self) -> None:
        wav = self._body()
        if not wav:
            self._json(400, {"error": "пустое тело запроса"})
            return
        try:
            text, sure = self.server.cfg.whisper.transcribe(wav)
        except Exception as e:
            log.exception("распознавание не вышло")
            self._json(500, {"error": str(e)})
            return
        # Уверенность едет вместе с текстом: по ней робот решает, выполнять
        # услышанное или переспросить. Так делают все, у кого команда может
        # что-то сдвинуть с места.
        self._json(200, {"text": text, "sure": sure})

    # --- разговор ---
    def _messages(self) -> None:
        cfg = self.server.cfg
        try:
            req = json.loads(self._body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, self._error("invalid_request_error", f"тело не JSON: {e}"))
            return

        messages = to_ollama_messages(req.get("system"), req.get("messages") or [])
        if not getattr(cfg.ollama, "think", False):
            # Мягкий выключатель размышлений. Параметр think понимают не все
            # сборки Ollama, а эту строку Qwen3 понимает сам — и без неё он
            # сначала пишет полстраницы рассуждений, даже когда параметр
            # выставлен. Дешевле сказать обоими способами.
            messages = _no_think(messages)
        tools = to_ollama_tools(req.get("tools"))
        # Имя модели из настроек робота игнорируем намеренно: там может стоять
        # облачное, а здесь запускается то, что реально скачано на этом ПК.
        model = cfg.model
        limit = int(req.get("max_tokens") or 1024)

        if req.get("stream"):
            self._stream(model, messages, tools, limit)
        else:
            self._whole(model, messages, tools, limit)

    @staticmethod
    def _error(kind: str, message: str) -> dict:
        return {"type": "error", "error": {"type": kind, "message": message}}

    def _collect(self, model, messages, tools, limit):
        """Гоняет Ollama и отдаёт куски: («text», строка) и («call», имя, аргументы)."""
        used_in = used_out = 0
        truncated = False
        unthink = Unthink()
        for part in self.server.cfg.ollama.chat(model, messages, tools, limit):
            msg = part.get("message") or {}
            chunk = msg.get("content") or ""
            if chunk:
                clean = unthink.feed(chunk)
                if clean:
                    yield ("text", clean, None)
            for call in msg.get("tool_calls") or []:
                # Придержанный текст выпускаем ПЕРЕД вызовом инструмента:
                # иначе «сейчас гляну» уедет за спину действия, и робот
                # объявит о сделанном раньше, чем скажет, что делает.
                # Размышления к этому моменту в любом случае кончились —
                # вызов инструмента идёт после них.
                tail = unthink.close()
                if tail:
                    yield ("text", tail, None)
                fn = call.get("function") or {}
                yield ("call", fn.get("name", ""), fn.get("arguments"))
            if part.get("done"):
                used_in = int(part.get("prompt_eval_count") or 0)
                used_out = int(part.get("eval_count") or 0)
                truncated = part.get("done_reason") == "length"
        tail = unthink.close()
        if tail:
            yield ("text", tail, None)
        yield ("done", used_in, (used_out, truncated))

    def _stream(self, model, messages, tools, limit) -> None:
        out = AnthropicStream(model)
        started = False
        try:
            for kind, a, b in self._collect(model, messages, tools, limit):
                if not started:
                    # Заголовки шлём только когда Ollama точно ответила: если
                    # она молчит, клиент должен увидеть честную ошибку, а не
                    # успешный ответ, оборванный на первом же байте.
                    started = True
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self._write(out.start())
                if kind == "text":
                    self._write(out.text(a))
                elif kind == "call":
                    self._write(out.tool_call(a, b))
                else:
                    used_out, truncated = b
                    self._write(out.finish(a, used_out, truncated))
        except Exception as e:
            log.exception("разговор не вышел")
            if not started:
                self._json(502, self._error("api_error", f"Ollama: {e}"))
            # Начали отдавать — заголовки уже ушли, сказать об ошибке нечем.
            # Просто закрываем: клиент увидит обрыв и уйдёт в облако.
            self.close_connection = True

    def _write(self, chunks) -> None:
        for chunk in chunks:
            self.wfile.write(chunk)
        self.wfile.flush()

    def _whole(self, model, messages, tools, limit) -> None:
        """Без потока. Нужен для проверки curl-ом и для простых клиентов."""
        blocks: list[dict] = []
        said: list[str] = []
        used_in = used_out = 0
        truncated = False
        try:
            for kind, a, b in self._collect(model, messages, tools, limit):
                if kind == "text":
                    said.append(a)
                elif kind == "call":
                    args = b
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    blocks.append({"type": "tool_use",
                                   "id": f"toolu_{len(blocks)}",
                                   "name": a, "input": args or {}})
                else:
                    used_in, (used_out, truncated) = a, b
        except Exception as e:
            log.exception("разговор не вышел")
            self._json(502, self._error("api_error", f"Ollama: {e}"))
            return

        text = "".join(said).strip()
        content = ([{"type": "text", "text": text}] if text else []) + blocks
        self._json(200, {
            "id": f"msg_{int(time.time()*1000):x}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "tool_use" if blocks else ("max_tokens" if truncated else "end_turn"),
            "stop_sequence": None,
            "usage": {"input_tokens": used_in, "output_tokens": used_out},
        })


class Config:
    def __init__(self, model: str, whisper: Whisper, ollama: Ollama) -> None:
        self.model = model
        self.whisper = whisper
        self.ollama = ollama


def main() -> int:
    p = argparse.ArgumentParser(description="Мозг робота на домашнем ПК")
    p.add_argument("--model", default="qwen3:4b",
                   help="имя модели в Ollama (ollama list покажет скачанные)")
    p.add_argument("--whisper", default="small",
                   help="размер модели распознавания: tiny|base|small|medium|large-v3")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--host", default="0.0.0.0",
                   help="0.0.0.0 — слышно роботу по сети, 127.0.0.1 — только этой машине")
    p.add_argument("--ollama", default=OLLAMA)
    p.add_argument("--think", action="store_true",
                   help="разрешить модели размышлять вслух: точнее с "
                        "инструментами, но ответ идёт в разы дольше")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    # Библиотеки скачивания и HTTP на уровне INFO пишут по строке на каждый
    # запрос, и наши сообщения в этом тонут — а окно сервера человек читает
    # именно чтобы понять, что происходит. С --debug всё возвращается.
    if not args.debug:
        for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub",
                      "filelock", "faster_whisper"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    ollama = Ollama(args.ollama, think=args.think)
    cfg = Config(args.model, Whisper(args.whisper), ollama)

    if not ollama.alive():
        log.warning("Ollama по адресу %s не отвечает. Запустите её и оставьте "
                    "висеть в трее — без неё разговаривать не с чем.", args.ollama)
    else:
        have = ollama.models()
        if not any(m == args.model or m.startswith(args.model + ":") for m in have):
            log.warning("модель %s не скачана. Скачать: ollama pull %s",
                        args.model, args.model)
            log.warning("сейчас есть: %s", ", ".join(have) or "ничего")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.cfg = cfg
    srv.daemon_threads = True
    log.info("мозг на %s:%d | модель %s | распознавание %s | сборка %s",
             args.host, args.port, args.model, args.whisper, _build())
    log.info("на роботе: ROBOT_PC_URL=http://<адрес этого ПК>:%d", args.port)

    # Прогрев в своём потоке: сервер должен отвечать на /health сразу, а вот
    # первая фраза не должна ждать загрузки моделей. Робот ждёт ответа
    # двадцать пять секунд, а холодный старт занимает больше — и тогда он
    # уходит в облако и не возвращается к ПК целую минуту.
    def warm() -> None:
        cfg.whisper.warm()
        if ollama.alive():
            ollama.warm(args.model)
        log.info("прогрет и готов — можно говорить")

    threading.Thread(target=warm, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
