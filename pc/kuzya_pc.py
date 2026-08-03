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
  POST /tts           текст → звук WAV. Silero вместо piper: сам ставит
                      ударения, различает омографы и поднимает интонацию на
                      вопросе. Робот говорит голосом, а не диктором вокзала.
  POST /voice/enroll  запомнить голос человека (имя в запросе, wav в теле).
                      Робот шлёт сюда несколько фраз подряд, и слепок
                      уточняется каждой.
  POST /voice/forget  забыть голос.
  GET  /health        что живо: Ollama, модель, Whisper, голос, кого узнаём.

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

# Windows без режима разработчика не умеет символьные ссылки, и библиотека
# скачивания честно предупреждает об этом на пол-экрана при каждом запуске.
# Нам это безразлично: модель одна, дублировать нечего, лишнего места она не
# займёт. Ставится до импорта самой библиотеки — позже уже не читается.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

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

# Как часто подавать знак жизни, пока сказать нечего. У робота таймаут чтения
# двадцать пять секунд, а размышления модели наружу не выходят — без пинга он
# считает молчащий ПК мёртвым и уходит в платное облако прямо посреди ответа.
PING_SECONDS = 5.0

# Сколько от старта считаем, что модель ещё грузится, и отвечаем сами. Загрузка
# четырёхмиллиардной модели с диска в видеопамять заняла на живом ПК семьдесят
# шесть секунд. Дальше этого срока молча ждать нельзя: если Ollama не поднялась
# вовсе, робот должен узнать правду и уйти в облако.
WARMING_GRACE = 240.0

# Что робот скажет вслух, пока мозг просыпается. Дешевле любого облака и
# честнее молчания: человек слышит, что его услышали.
WARMING_REPLY = "Секунду, я ещё просыпаюсь."

# Распознавание по умолчанию — русское дообучение turbo-версии Whisper.
#
# Почему не стандартная. Whisper учили на всех языках сразу, и русский в нём
# идёт довеском: отсюда «водильник» вместо будильника и «Пусть за» вместо
# «Кузя». Дообученные на русском веса грузятся тем же самым вызовом — для кода
# это просто другое имя, — а слов путают заметно меньше.
#
# Почему turbo. У неё урезан декодер: четыре слоя вместо тридцати двух. Качество
# как у large, а по скорости она обгоняет medium — то есть мы берём модель
# лучше и быстрее одновременно. Видеопамяти ей нужно около двух гигабайт: на
# шестигигабайтной карте это влезает вместе с четырёхмиллиардной моделью.
#
# Если склад с ней недоступен, поднимется FALLBACK_WHISPER — робот не должен
# глохнуть из-за чужого сайта.
DEFAULT_WHISPER = "dvislobokov/faster-whisper-large-v3-turbo-russian"
FALLBACK_WHISPER = "medium"

# Голос. Пятая версия русской модели silero: сама ставит ударения, различает
# омографы и поднимает интонацию на вопросе. Сто сорок мегабайт, считает на
# процессоре быстрее реального времени.
SILERO = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"


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


# Мягкого выключателя /no_think здесь НЕТ, и это осознанно. Он был — сначала
# в системном сообщении, потом в последней реплике человека, — и не сработал
# ни разу. Проверка прямым запросом к Ollama показала почему: свежие Qwen3
# разъехались на два отдельных выпуска, думающий и нет, и думающий команду
# просто не знает. Хуже того, он на неё отвечает: на «привет /no_think» модель
# сказала «Привет! Но я не понимаю команду /no_think». То есть строка не
# выключала размышления, а портила фразу человека. Не возвращать.


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


class RobotGone(Exception):
    """Робот закрыл соединение, не дослушав ответ."""


class Unthink:
    """Отрезает размышления модели, даже когда она их не открыла.

    Qwen3 не пишет <think> в ответе: этот тег уже стоит в шаблоне запроса,
    поэтому генерация начинается сразу внутри размышлений, а наружу выходит
    только закрывающий </think>. Фильтр, который ищет пару тегов, такое
    пропускает целиком — на живом роботе он зачитал вслух полторы страницы
    рассуждений про то, каким должен быть ответ.

    Поэтому начало ответа придерживаем: увидели </think> — всё, что было до
    него, выбрасываем.

    Первая версия отпускала начало через четыреста символов — мол, если
    размышлений нет в начале, их нет вовсе. На живом роботе размышления
    оказались в пять раз длиннее, и он зачитал их вслух до последнего слова.
    Порога, отличающего «размышлений нет» от «размышления длинные», не бывает:
    и то и другое выглядит как текст без тега.

    Значит, порога нет, а есть привычка. Думает модель вслух или нет —
    свойство модели, а не отдельного ответа: выясняется по первому же ответу и
    запоминается в habit на весь запуск сервера. Известного молчуна дальше
    отдаём сразу, без задержки; про известного болтуна ждём тег сколько нужно.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self, habit: dict | None = None, key: str = "") -> None:
        self.habit = habit if habit is not None else {}
        self.key = key
        self.buf = ""
        self.holding = self.habit.get(key) is not False

    def _learn(self, thinks: bool) -> None:
        was = self.habit.get(self.key)
        if was is thinks:
            return
        # Учимся несимметрично. «Думает» — доказанный факт: тег видели своими
        # глазами. «Не думает» — всего лишь отсутствие улики, и принимаем его
        # только пока ничего не знаем. Иначе один оборванный ответ без тега
        # переубеждает фильтр, он перестаёт держать начало — и следующие
        # размышления едут прямиком в речь.
        if not thinks and was is not None:
            return
        log.info("модель %s вслух", "думает" if thinks else "не думает")
        self.habit[self.key] = thinks

    def feed(self, chunk: str) -> str:
        if not self.holding:
            return chunk
        self.buf += chunk
        at = self.buf.find(self.CLOSE)
        if at < 0:
            return ""
        opened = self.buf.find(self.OPEN)
        out, self.buf, self.holding = self.buf, "", False
        if 0 <= opened < at:
            # Обычная пара тегов: начало ответа — настоящий текст. Отдаём как
            # есть, разберёт фильтр на стороне робота.
            return out
        self._learn(True)
        return out[at + len(self.CLOSE):].lstrip()

    def close(self, complete: bool = True) -> str:
        """Хвост, который так и не оказался размышлением.

        complete=False — ответ оборвался (клиент ушёл, кончился лимит длины).
        Из такого ответа нельзя заключить, что модель не думает: тег мог быть
        в той части, которая не сгенерировалась.
        """
        if self.holding and complete:
            self._learn(False)
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

    def ping(self):
        """Знак жизни, пока сказать нечего.

        Размышления модели наружу не выходят, и на всё это время поток
        замолкает — на живом роботе на двадцать пять секунд, после чего у него
        срабатывал таймаут чтения, он бросал бесплатный ПК и уходил в платное
        облако. Пинг — часть протокола: клиент его молча съедает, а соединение
        считается живым.
        """
        yield _sse("ping", {"type": "ping"})

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
        # Что выяснилось на деле: думает ли модель вслух, несмотря на всё
        # вышесказанное. Ключ — имя модели, значение ставит Unthink.
        self.habit: dict[str, bool] = {}
        # Разбор первого ответа печатаем один раз: кто из троих не сработал —
        # параметр think, строка /no_think или сама модель — из обычного лога
        # не видно, а гадать об этом дорого.
        self._explained = False
        # Модель ещё грузится в видеопамять. Ставится прогревом при старте.
        self.ready = False
        self.started = time.monotonic()

    def explain(self, model: str, split: bool, thought: bool) -> None:
        """Разбирается с размышлениями по первому же ответу.

        split — Ollama отдала размышления отдельным полем: значит она разбирает
        их сама, и content приходит чистым.
        thought — размышления всё-таки были.

        Главное открытие живого сеанса: think=false у Ollama означает «не
        разбирай размышления», а вовсе не «пусть модель не думает». Модель
        думает ровно столько же, но её рассуждения летят прямо в content
        вместе с закрывающим тегом. То есть выключатель делает хуже, чем его
        отсутствие. Поймали такое — включаем разбор обратно: рассуждения
        уедут в отдельное поле, а content станет чистым.
        """
        if self._explained:
            return
        self._explained = True
        if not thought:
            log.info("размышления выключены (think=%s)", self.think)
            return
        if split:
            log.warning("модель думает, несмотря на think=%s, но Ollama "
                        "разбирает размышления сама — наружу они не идут. "
                        "Плата за это — время и токены на каждый ответ",
                        self.think)
            return
        log.warning("модель думает вслух прямо в тексте, и выключить это "
                    "нечем: think=%s Ollama понимает как «не разбирать», а не "
                    "как «не думать». Включаю разбор обратно — рассуждения "
                    "уедут в отдельное поле. Насовсем это лечится только "
                    "нерассуждающей моделью: ollama pull "
                    "qwen3:4b-instruct-2507-q4_K_M", self.think)
        self.think = True
        # Привычку забываем: с этого момента content чистый, и держать его
        # начало, дожидаясь тега, которого больше не будет, незачем.
        self.habit.pop(model, None)

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
# Заученные выдумки. Whisper учили на субтитрах с ютуба, и на тишине или шуме
# он выдаёт оттуда самые частые концовки роликов. На живом роботе за один
# вечер пришли «Спасибо за внимание!», «С вами был Игорь Негода» и
# «Продолжение следует…» — робот отвечал на них вслух, разговаривая с
# холодильником. Уверенность при этом бывает приличная (-0.73), так что
# барьером это не ловится: модель не сомневается, она вспоминает.
_MADE_UP = (
    "спасибо за внимание", "с вами был", "субтитры", "продолжение следует",
    "редактор субтитров", "корректор", "все права защищены",
    "подписывайтесь на канал", "ставьте лайки", "до новых встреч",
    "спасибо за просмотр", "перевод и озвучание", "фонд кино",
)


def made_up(text: str) -> bool:
    """Похоже ли услышанное на заученную концовку ролика, а не на речь."""
    bare = text.lower().replace("ё", "е").strip(" .,!?…-«»\"")
    return any(bare.startswith(p) or bare == p for p in _MADE_UP)


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
        """Грузит запрошенную модель, а если её нет — запасную.

        Модель может быть не только «small» или «medium», но и чужим складом
        на HuggingFace: русские дообучения Whisper там лежат и грузятся тем же
        вызовом. Дело хорошее, но чужой склад может переехать или закрыться, а
        робот от этого глохнуть не должен — поэтому под ним подстелена обычная
        modelка из стандартного набора.
        """
        try:
            return self._try(self.size)
        except Exception as e:
            if self.size == FALLBACK_WHISPER:
                raise
            log.warning("модель распознавания %r не поднялась (%s) — беру %s",
                        self.size, e, FALLBACK_WHISPER)
            model = self._try(FALLBACK_WHISPER)
            self.size = FALLBACK_WHISPER
            return model

    def _try(self, size: str):
        from faster_whisper import WhisperModel

        # Сначала видеокарта: ради неё всё и затевалось. Если CUDA нет или
        # библиотеки не встали — честно отступаем на процессор. Даже он на
        # настольной машине быстрее, чем Cortex-A55 на роботе.
        for device, compute in (("cuda", "float16"), ("cpu", "int8")):
            try:
                model = WhisperModel(size, device=device, compute_type=compute)
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
                        model = WhisperModel(size, device=device,
                                             compute_type=compute)
                    except Exception as again:
                        log.warning("whisper на %s не поднялся (%s)", device, again)
                        continue
                else:
                    log.warning("whisper на %s не поднялся (%s)", device, e)
                    continue
            self.device = device
            log.info("whisper: модель %s на %s", size, device)
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
        if made_up(text):
            log.info("выдумал концовку ролика (%r) — считаю тишиной", text)
            text, sure = "", None
        spent = time.monotonic() - started
        length = getattr(info, "duration", 0.0) or 0.0
        log.info("whisper: %.2f с на %.1f с звука (×%.2f) | уверенность %s → %r",
                 spent, length, spent / length if length else 0,
                 f"{sure:.2f}" if sure is not None else "—", text)
        return text, sure


# --------------------------------------------------------------------------
# Кто говорит
# --------------------------------------------------------------------------
class Voiceprints:
    """Узнаёт человека по голосу.

    Работает не на словах, а на тембре: ECAPA-TDNN сворачивает любую фразу в
    вектор из двух сотен чисел, и у одного человека эти векторы лежат кучно, а
    у разных людей — врозь. Сравнение — косинус между векторами.

    Зачем это роботу. Во-первых, чтобы не отвечать телевизору: голос диктора
    не совпадёт ни с чьим слепком. Во-вторых, чтобы знать, с кем разговаривает,
    и держать на каждого своё личное дело.

    Слепки лежат здесь, на ПК, — там же, где считаются. Личные дела живут на
    роботе: они нужны ему для разговора и тогда, когда ПК выключен.

    Порог намеренно строгий. Ошибиться в сторону «не узнал» дёшево: робот
    переспросит. Ошибиться в другую — значит показать одному человеку записи
    про другого.
    """

    # Косинус между слепками. У ECAPA свой человек обычно даёт 0.7 и выше,
    # чужой — 0.3 и ниже. Настоящее число подберём по живому логу: похожесть
    # пишется в каждый ответ ровно ради этого.
    SAME = 0.62
    RATE = 16000
    MODEL = "speechbrain/spkrec-ecapa-voxceleb"

    # Сколько не трогать модель после неудачной загрузки. Без этого каждая
    # фраза заново лезла на HuggingFace, падала и стоила восьми секунд — то
    # есть сломанное узнавание делало медленным ВСЁ распознавание.
    RETRY_AFTER = 600.0

    def __init__(self, store: Path) -> None:
        self.store = store
        self._model = None
        self._lock = threading.Lock()
        self.people: dict[str, dict] = {}
        self.ready = False
        self._broken_until = 0.0
        self._read()

    # --- хранение --------------------------------------------------------
    def _read(self) -> None:
        try:
            self.people = json.loads(self.store.read_text("utf-8"))
        except (OSError, ValueError):
            self.people = {}

    def _write(self) -> None:
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.people, ensure_ascii=False), "utf-8")
            tmp.replace(self.store)
        except OSError as e:
            log.warning("не сохранил слепки голосов (%s)", e)

    # --- модель ----------------------------------------------------------
    def _load(self):
        from speechbrain.inference.speaker import EncoderClassifier

        where = Path(os.environ.get("HF_HOME", Path.home() / ".cache")) / "ecapa"
        common = dict(source=self.MODEL, savedir=str(where),
                      run_opts={"device": "cpu"})
        # На Windows библиотека раскладывает файлы модели символьными ссылками,
        # а прав на них у обычного пользователя нет: «Клиент не обладает
        # требуемыми правами». Просим копировать. Параметр появился не во всех
        # версиях, поэтому при отказе пробуем по-старому.
        try:
            from speechbrain.utils.fetching import LocalStrategy
            return EncoderClassifier.from_hparams(
                local_strategy=LocalStrategy.COPY, **common)
        except (ImportError, TypeError, AttributeError):
            return EncoderClassifier.from_hparams(**common)

    def warm(self) -> None:
        started = time.monotonic()
        with self._lock:
            if self._model is None:
                try:
                    self._model = self._load()
                except Exception as e:
                    self._broken_until = time.monotonic() + self.RETRY_AFTER
                    log.warning("узнавание по голосу не поднялось (%s) — робот "
                                "будет считать всех одним человеком", e)
                    return
        self.ready = True
        log.info("узнаю по голосу %d: %s, прогрев занял %.0f с",
                 len(self.people), ", ".join(self.people) or "никого",
                 time.monotonic() - started)

    def _vector(self, wav: bytes):
        """Слепок голоса из wav. None — фраза слишком коротка или модели нет."""
        import io
        import wave as wavelib

        import numpy as np
        import torch

        with wavelib.open(io.BytesIO(wav)) as w:
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        if w.getnchannels() > 1:
            pcm = pcm[::w.getnchannels()]
        # Меньше секунды — на таком тембр не разобрать, и слепок выйдет
        # случайным. Лучше честно не узнать, чем узнать не того.
        if len(pcm) < rate:
            return None
        signal = pcm.astype("float32") / 32768.0
        if rate != self.RATE:
            # Простое прореживание по времени. Модель ждёт шестнадцать
            # килогерц, а робот может прислать что угодно.
            index = np.linspace(0, len(signal) - 1, int(len(signal) * self.RATE / rate))
            signal = np.interp(index, np.arange(len(signal)), signal).astype("float32")

        with self._lock:
            if self._model is None:
                # Модель уже не поднялась и повторять рано. Раньше повторяли на
                # КАЖДОЙ фразе: попытка лезла на HuggingFace, падала и стоила
                # восьми секунд, то есть сломанное узнавание делало медленным
                # всё распознавание разом.
                if time.monotonic() < self._broken_until:
                    return None
                try:
                    self._model = self._load()
                except Exception:
                    self._broken_until = time.monotonic() + self.RETRY_AFTER
                    raise
                self.ready = True
            vector = self._model.encode_batch(torch.from_numpy(signal).unsqueeze(0))
        vector = vector.squeeze().detach().numpy()
        return vector / (float(np.linalg.norm(vector)) or 1.0)

    # --- работа ----------------------------------------------------------
    def identify(self, wav: bytes) -> tuple[str, float]:
        """Кто это сказал и насколько похоже. Пустое имя — не узнал."""
        if not self.people:
            return "", 0.0
        try:
            mine = self._vector(wav)
        except Exception as e:
            log.warning("не смог снять слепок голоса (%s)", e)
            return "", 0.0
        if mine is None:
            return "", 0.0

        import numpy as np

        best, score = "", -1.0
        for name, card in self.people.items():
            near = float(np.dot(mine, np.asarray(card["вектор"], dtype="float32")))
            if near > score:
                best, score = name, near
        if score < self.SAME:
            log.info("голос не опознан (ближе всех %s: %.2f)", best, score)
            return "", score
        return best, score

    def enroll(self, name: str, wav: bytes) -> int:
        """Добавляет фразу к слепку человека. Возвращает, сколько их всего."""
        mine = self._vector(wav)
        if mine is None:
            raise ValueError("фраза короче секунды — по такой голос не запомнить")

        import numpy as np

        card = self.people.get(name) or {"вектор": [0.0] * len(mine), "фраз": 0}
        было = np.asarray(card["вектор"], dtype="float32") * card["фраз"]
        # Скользящее среднее: каждая новая фраза уточняет слепок, а не
        # заменяет его. Один зевок или кашель тогда не портит всё.
        средний = (было + mine) / (card["фраз"] + 1)
        средний = средний / (float(np.linalg.norm(средний)) or 1.0)
        self.people[name] = {"вектор": [float(x) for x in средний],
                             "фраз": card["фраз"] + 1}
        self._write()
        log.info("голос %s запомнен, фраз в слепке: %d",
                 name, self.people[name]["фраз"])
        return self.people[name]["фраз"]

    def forget(self, name: str) -> bool:
        if name not in self.people:
            return False
        del self.people[name]
        self._write()
        log.info("слепок голоса %s забыт", name)
        return True


# --------------------------------------------------------------------------
# Голос
# --------------------------------------------------------------------------
class Voice:
    """Синтез речи на ПК.

    На роботе стоит piper, и по-своему он хорош: работает без интернета и
    почти ничего не весит. Но крутится он на Cortex-A55, то есть каждая фраза
    сначала считается, и только потом звучит. И голос у него ровный, как у
    диктора вокзала: точку от вопроса не отличить.

    Здесь — silero. Русская модель, сто сорок мегабайт, считает на процессоре
    быстрее реального времени. Сама расставляет ударения, различает омографы
    («зАмок» и «замОк») и — главное для живой речи — поднимает интонацию на
    вопросе. Робот наконец звучит как собеседник, а не как автоответчик.

    Видеопамять не трогаем намеренно: там уже сидят модель разговора и
    распознавание, и на шестигигабайтной карте свободного места нет. Процессор
    в это время всё равно простаивает.
    """

    # Голоса модели. Первый — по умолчанию: мужской, спокойный, ровный.
    VOICES = ("eugene", "aidar", "baya", "kseniya", "xenia")
    RATE = 24000

    def __init__(self, url: str = SILERO, speaker: str = "eugene") -> None:
        self.url = url
        self.speaker = speaker if speaker in self.VOICES else self.VOICES[0]
        self._model = None
        self._lock = threading.Lock()
        self.ready = False

    def _load(self):
        import torch

        # Пакет качаем сами, а не через torch.hub: тот тянет весь репозиторий
        # с гитхаба, а нам нужен один файл. Кладём рядом с моделями Whisper.
        where = Path(os.environ.get("HF_HOME", Path.home() / ".cache"))
        where.mkdir(parents=True, exist_ok=True)
        package = where / self.url.rsplit("/", 1)[-1]
        if not package.exists():
            log.info("качаю голос %s", package.name)
            torch.hub.download_url_to_file(self.url, str(package))
        model = torch.package.PackageImporter(
            str(package)).load_pickle("tts_models", "model")
        # На процессоре намеренно: см. описание класса. Потоков даём немного —
        # синтез и так быстрее реального времени, а лишние только мешают
        # распознаванию, которое живёт в этом же процессе.
        model.to(torch.device("cpu"))
        torch.set_num_threads(max(2, (os.cpu_count() or 4) // 2))
        return model

    def warm(self) -> None:
        started = time.monotonic()
        with self._lock:
            if self._model is None:
                try:
                    self._model = self._load()
                except Exception as e:
                    log.warning("голос не поднялся (%s) — робот будет говорить "
                                "своим piper", e)
                    return
        # Первая настоящая фраза иначе идёт вдесятеро дольше остальных: модель
        # загружена, но графы вычислений строятся при первом же проходе. На
        # живом роботе это дало 1.3 секунды на «Кузя на связи» против сотых
        # долей потом. Прогреваем настоящим синтезом, звук выбрасываем.
        try:
            self._model.apply_tts(text="раз два три", speaker=self.speaker,
                                  sample_rate=self.RATE)
        except Exception as e:
            log.warning("голос загрузился, но не синтезирует (%s)", e)
            return
        self.ready = True
        log.info("голос %s готов, прогрев занял %.0f с",
                 self.speaker, time.monotonic() - started)

    def say(self, text: str, speaker: str = "") -> bytes:
        """Синтезирует фразу и отдаёт её готовым wav-файлом."""
        import numpy as np

        with self._lock:
            if self._model is None:
                self._model = self._load()
                self.ready = True
            started = time.monotonic()
            who = speaker if speaker in self.VOICES else self.speaker
            audio = self._model.apply_tts(text=text, speaker=who,
                                          sample_rate=self.RATE)

        raw = (np.asarray(audio, dtype="float32").clip(-1.0, 1.0) * 32767
               ).astype("<i2").tobytes()
        spent = time.monotonic() - started
        length = len(raw) / 2 / self.RATE
        log.info("голос: %.2f с на %.1f с речи (×%.2f) → %r",
                 spent, length, spent / length if length else 0, text[:60])
        return _wav(raw, self.RATE)


def _wav(raw: bytes, rate: int) -> bytes:
    """Оборачивает сырой звук в wav-заголовок. Без внешних библиотек."""
    import struct

    header = b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(raw)) + raw


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
                # Робот спрашивает это при старте: есть голос на ПК или
                # говорить своим piper.
                "голос": getattr(getattr(cfg, "voice", None), "ready", False),
                "узнаю_по_голосу": sorted(
                    getattr(getattr(cfg, "who", None), "people", {})),
                # Часы. Робот сверяет их со своими: на его SBC нет батарейки
                # часов, и после выключения питания время уезжает на часы. А от
                # него зависят будильники, напоминания и тихие часы.
                #
                # Строка — для человека, в лог. Сверять по ней НЕЛЬЗЯ: робот и
                # ПК могут стоять в разных поясах и показывать разное время,
                # будучи при этом идеально синхронными. Ровно так и вышло:
                # робот в Калининграде, ПК по Москве — час разницы на ровном
                # месте. Сверяют по числу секунд, оно от пояса не зависит.
                "часы": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "секунд": round(time.time()),
            })
            return
        self._json(404, {"error": "нет такого адреса"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/v1/messages") or path.endswith("/messages"):
            self._messages()
        elif path.endswith("/stt"):
            self._stt()
        elif path.endswith("/tts"):
            self._tts()
        elif path.endswith("/voice/enroll"):
            self._enroll()
        elif path.endswith("/voice/forget"):
            self._forget()
        else:
            self._json(404, {"error": "нет такого адреса"})

    # --- кто говорит ---
    def _who(self):
        return getattr(self.server.cfg, "who", None)

    def _name_asked(self) -> str:
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(self.path).query).get("имя", [""])[0]
                or parse_qs(urlparse(self.path).query).get("name", [""])[0]).strip()

    def _enroll(self) -> None:
        who = self._who()
        if who is None:
            self._json(503, {"error": "узнавание по голосу не поднято"})
            return
        name = self._name_asked()
        wav = self._body()
        if not name or not wav:
            self._json(400, {"error": "нужны имя в запросе и wav в теле"})
            return
        try:
            фраз = who.enroll(name, wav)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        except Exception as e:
            log.exception("не смог запомнить голос")
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"кто": name, "фраз": фраз})

    def _forget(self) -> None:
        who = self._who()
        if who is None:
            self._json(503, {"error": "узнавание по голосу не поднято"})
            return
        name = self._name_asked()
        self._json(200, {"кто": name, "забыт": bool(name) and who.forget(name)})

    # --- голос ---
    def _tts(self) -> None:
        voice = getattr(self.server.cfg, "voice", None)
        if voice is None:
            self._json(503, {"error": "голос на этом ПК не поднят"})
            return
        try:
            req = json.loads(self._body().decode("utf-8"))
            text = (req.get("text") or "").strip()
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"error": f"тело не JSON: {e}"})
            return
        if not text:
            self._json(400, {"error": "пустая фраза"})
            return
        try:
            wav = voice.say(text, req.get("voice") or "")
        except Exception as e:
            # Робот на это отвечает переходом на свой piper — то есть говорить
            # он не перестанет, просто прежним голосом.
            log.exception("синтез не вышел")
            self._json(500, {"error": str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        try:
            self.wfile.write(wav)
        except (ConnectionError, BrokenPipeError):
            log.info("робот отключился, не дослушав")
            self.close_connection = True

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
        # Кто это сказал. Робот по этому решает, с кем разговаривает, и не
        # отвечает ли он телевизору.
        кто, похожесть = "", 0.0
        who = self._who()
        if who is not None and text:
            кто, похожесть = who.identify(wav)
        self._json(200, {"text": text, "sure": sure,
                         "кто": кто, "похожесть": round(похожесть, 3)})

    # --- разговор ---
    def _messages(self) -> None:
        cfg = self.server.cfg
        try:
            req = json.loads(self._body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, self._error("invalid_request_error", f"тело не JSON: {e}"))
            return

        messages = to_ollama_messages(req.get("system"), req.get("messages") or [])
        tools = to_ollama_tools(req.get("tools"))
        # Имя модели из настроек робота игнорируем намеренно: там может стоять
        # облачное, а здесь запускается то, что реально скачано на этом ПК.
        model = cfg.model
        limit = int(req.get("max_tokens") or 1024)

        if self._warming():
            # Модель ещё едет с диска в видеопамять. Запрос сейчас встанет в
            # очередь за загрузкой и не уложится в двадцать пять секунд, что
            # робот отводит на ответ, — он решит, что ПК умер, и уйдёт в
            # платное облако. На живом роботе одно «привет», сказанное в эту
            # минуту, стоило девять тысяч оплаченных токенов. Отвечаем сами:
            # бесплатно, мгновенно и честно.
            log.info("ещё прогреваюсь — отвечаю сам, не пуская робота в облако")
            if req.get("stream"):
                self._stream_text(model, WARMING_REPLY)
            else:
                self._whole_text(model, WARMING_REPLY)
            return

        if req.get("stream"):
            self._stream(model, messages, tools, limit)
        else:
            self._whole(model, messages, tools, limit)

    def _warming(self) -> bool:
        """Модель ещё грузится, и ждать её дольше, чем ждёт робот.

        Срок ограничен: если Ollama не поднялась вовсе, вечно отвечать
        «просыпаюсь» нельзя — робот должен узнать правду и уйти в облако.
        """
        ollama = self.server.cfg.ollama
        if getattr(ollama, "ready", True):
            return False
        return time.monotonic() - getattr(ollama, "started", 0.0) < WARMING_GRACE

    @staticmethod
    def _error(kind: str, message: str) -> dict:
        return {"type": "error", "error": {"type": kind, "message": message}}

    def _collect(self, model, messages, tools, limit):
        """Гоняет Ollama и отдаёт куски: («text», строка) и («call», имя, аргументы)."""
        used_in = used_out = 0
        truncated = False
        ollama = self.server.cfg.ollama
        unthink = Unthink(getattr(ollama, "habit", None), model)
        alive = time.monotonic()
        split = False
        for part in ollama.chat(model, messages, tools, limit):
            msg = part.get("message") or {}
            # Размышления отдельным полем — так их отдаёт Ollama, когда знает
            # модель как размышляющую. Наружу они не идут никогда.
            if msg.get("thinking"):
                split = True
            chunk = msg.get("content") or ""
            if chunk:
                clean = unthink.feed(chunk)
                if clean:
                    alive = time.monotonic()
                    yield ("text", clean, None)
                elif time.monotonic() - alive > PING_SECONDS:
                    # Модель думает, а наружу мы это не пускаем. Молчать нельзя:
                    # у робота таймаут чтения, и он уйдёт в платное облако.
                    alive = time.monotonic()
                    yield ("ping", None, None)
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
        # Придержанное начало на конце ответа — это норма: пока про модель
        # ничего не известно, мы держим всё. Тревожно другое — когда держали
        # долго и много: значит либо модель думает без закрывающего тега, либо
        # ответ обрезали посреди размышлений.
        if unthink.holding and (truncated or len(unthink.buf) > 400):
            log.warning("ответ кончился, а размышления не закрылись — "
                        "%d символов придержано, %d токенов выхода%s",
                        len(unthink.buf), used_out,
                        ", ответ обрезан по лимиту длины" if truncated else "")
        tail = unthink.close(not truncated)
        if hasattr(ollama, "explain"):
            ollama.explain(model, split, split or unthink.habit.get(model) is True)
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
                elif kind == "ping":
                    self._write(out.ping())
                elif kind == "call":
                    self._write(out.tool_call(a, b))
                else:
                    used_out, truncated = b
                    self._write(out.finish(a, used_out, truncated))
        except RobotGone as e:
            # Робот ушёл: не дождался, передумал, потерял сеть. Это будни, а не
            # авария — трассировка на полэкрана тут только мешает читать лог.
            log.info("робот отключился посреди ответа (%s)", e)
            self.close_connection = True
        except Exception as e:
            log.exception("разговор не вышел")
            if not started:
                self._json(502, self._error("api_error", f"Ollama: {e}"))
            # Начали отдавать — заголовки уже ушли, сказать об ошибке нечем.
            # Просто закрываем: клиент увидит обрыв и уйдёт в облако.
            self.close_connection = True

    def _stream_text(self, model: str, text: str) -> None:
        """Свой собственный ответ, потоком и по всем правилам протокола."""
        out = AnthropicStream(model)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._write(out.start())
            self._write(out.text(text))
            self._write(out.finish(0, 0, False))
        except RobotGone as e:
            log.info("робот отключился посреди ответа (%s)", e)
            self.close_connection = True

    def _whole_text(self, model: str, text: str) -> None:
        """То же самое, но одним куском."""
        self._json(200, {
            "id": f"msg_{int(time.time()*1000):x}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    def _write(self, chunks) -> None:
        """Отдаёт кусок потока роботу.

        Обрыв здесь и обрыв связи с Ollama — разные беды с одним и тем же
        именем ConnectionError, а лечатся они противоположно: про мёртвую
        Ollama роботу надо честно сказать 502, а про ушедшего робота говорить
        уже некому. Поэтому свой тип: ловить по месту, а не по имени.
        """
        try:
            for chunk in chunks:
                self.wfile.write(chunk)
            self.wfile.flush()
        except (ConnectionError, BrokenPipeError) as e:
            raise RobotGone(type(e).__name__) from e

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
                elif kind == "done":
                    used_in, (used_out, truncated) = a, b
                # «ping» держит живым поток; здесь потока нет и держать нечего.
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
    def __init__(self, model: str, whisper: Whisper, ollama: Ollama,
                 voice: Voice | None = None,
                 who: Voiceprints | None = None) -> None:
        self.model = model
        self.whisper = whisper
        self.ollama = ollama
        self.voice = voice
        self.who = who


def main() -> int:
    p = argparse.ArgumentParser(description="Мозг робота на домашнем ПК")
    p.add_argument("--model", default="qwen3:4b",
                   help="имя модели в Ollama (ollama list покажет скачанные)")
    p.add_argument("--whisper", default=DEFAULT_WHISPER,
                   help="модель распознавания: имя размера (tiny|base|small|"
                        "medium|large-v3) или склад на HuggingFace")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--host", default="0.0.0.0",
                   help="0.0.0.0 — слышно роботу по сети, 127.0.0.1 — только этой машине")
    p.add_argument("--ollama", default=OLLAMA)
    p.add_argument("--think", action="store_true",
                   help="разрешить модели размышлять вслух: точнее с "
                        "инструментами, но ответ идёт в разы дольше")
    p.add_argument("--voice", default=Voice.VOICES[0],
                   help="голос синтеза: " + "|".join(Voice.VOICES) +
                        " либо «нет», чтобы робот говорил своим piper")
    p.add_argument("--no-voiceprints", action="store_true",
                   help="не узнавать людей по голосу")
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
    рядом = Path(os.environ.get("HF_HOME", Path.home() / ".cache")) / "кузя"
    cfg = Config(args.model, Whisper(args.whisper), ollama,
                 None if args.voice == "нет" else Voice(speaker=args.voice),
                 None if args.no_voiceprints else Voiceprints(рядом / "голоса.json"))

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
    log.info("мозг на %s:%d | модель %s | распознавание %s | голос %s | "
             "сборка %s", args.host, args.port, args.model,
             args.whisper.rsplit("/", 1)[-1],
             args.voice if cfg.voice is not None else "робота", _build())
    log.info("на роботе: ROBOT_PC_URL=http://<адрес этого ПК>:%d", args.port)

    # Прогрев в своём потоке: сервер должен отвечать на /health сразу, а вот
    # первая фраза не должна ждать загрузки моделей. Робот ждёт ответа
    # двадцать пять секунд, а холодный старт занимает больше — и тогда он
    # уходит в облако и не возвращается к ПК целую минуту.
    # Врозь, а не по очереди. Распознавание встаёт за восемнадцать секунд,
    # модель — за семьдесят шесть, и пока они грузились друг за другом, робот
    # успевал распознать фразу, отправить её мозгу и не дождаться ответа. По
    # отдельности распознавание готово втрое раньше и сразу приносит пользу.
    def warm_whisper() -> None:
        cfg.whisper.warm()

    def warm_model() -> None:
        if ollama.alive():
            ollama.warm(args.model)
        else:
            log.warning("Ollama не отвечает — мозг работать не будет")
        ollama.ready = True
        log.info("прогрет и готов — можно говорить")

    def warm_voice() -> None:
        if cfg.voice is not None:
            cfg.voice.warm()

    def warm_who() -> None:
        if cfg.who is not None:
            cfg.who.warm()

    for job in (warm_whisper, warm_model, warm_voice, warm_who):
        threading.Thread(target=job, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
