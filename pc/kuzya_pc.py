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
  GET  /avatar        аватар Кузи (Live2D) в браузере — видеокарта у ПК уже
                      занята Ollama, ей не тяжело нарисовать и это.
                      См. pc/avatar/README.md.
  POST /avatar/state  сюда голос робота шлёт то же самое, что пишет в
                      face.json. Что с этим делать — решает не рисование
                      (страница в браузере), а та же логика позы, что и на
                      роботе (face/character.py), просто выполненная здесь.
  GET  /avatar/stream та же страница, снятая здесь же headless-браузером и
                      отданная роботу потоком JPEG-кадров (MJPEG). Экран
                      робота показывает ЭТО, а своего домовёнка рисует лишь
                      когда ПК недоступен: у робота нет видеокарты, и Live2D
                      на нём не нарисовать — зато картинку с ПК он покажет.
  GET  /avatar/frame.jpg  один последний кадр — глянуть в браузере, что робот
                      видит на самом деле.

Почему не LiteLLM. Он умеет то же самое, но это полтысячи мегабайт
зависимостей и отдельное окно, которое надо не закрыть. Здесь один файл,
который держит и разговор, и распознавание, — и на Windows это разница
между «работает» и «в прошлый раз я что-то забыл запустить».

Запуск:
    python kuzya_pc.py --model qwen3:4b --whisper small

Робот на своей стороне: ROBOT_PC_URL=http://адрес-этого-ПК:4000

Для потока аватара на экран робота нужен ещё Playwright (pip install
playwright): он поднимает headless-браузер — системный Edge или Chrome, если
есть, — и снимает с него кадры. Без него всё остальное работает как прежде,
а робот рисует лицо сам.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
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

# Кем робот смотрит. Разговорная qwen3:4b зрения не имеет вовсе, и делить их
# правильно: смотрит робот изредка, а разговаривает постоянно — держать ради
# редкого взгляда тяжёлую зрячую модель в разговоре значит платить памятью и
# скоростью за каждую фразу.
VISION_MODEL = os.environ.get("ROBOT_PC_VISION_MODEL", "qwen2.5vl:7b")

# Сколько ждём Ollama. Генерация идёт секунды, а вот дозвон должен быть
# быстрым: если Ollama не запущена, робот должен узнать об этом сразу и уйти
# в облако, а не молчать полминуты.
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
# Сколько токенов контекста просить у Ollama.
#
# Числа тут не с потолка. Постоянная часть запроса робота — системный промпт
# плюс схемы инструментов — это около двенадцати тысяч символов, причём
# половина из них кириллица, а она в токенизаторе дороже латиницы. Выходит
# примерно четыре тысячи токенов ДО того, как человек сказал хоть слово.
#
# Умолчание Ollama — 4096. То есть робот в него не помещался никогда: справка
# о собеседнике, восемь сообщений истории и сам ответ шли уже за край. А за
# краем Ollama молча выбрасывает начало — то самое, где записано, кто робот
# такой и как ему говорить. Ни ошибки, ни предупреждения: модель просто
# отвечает казённо и не помнит, о чём был разговор.
#
# Восемь тысяч закрывают постоянную часть с запасом на историю. Платим за это
# видеопамятью под KV-кэш — на 4B-модели это сотни мегабайт.
DEFAULT_CTX = 8192


class Ollama:
    def __init__(self, base: str = OLLAMA, *, think: bool = False,
                 ctx: int = DEFAULT_CTX) -> None:
        self.base = base.rstrip("/")
        self.ctx = max(2048, int(ctx))
        # Знает ли эта сборка chat_template_kwargs — единственный настоящий
        # выключатель размышлений у гибридных моделей. Выясняется по первому
        # отказу, как и think.
        self._kwargs_known = True
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

    def _options(self, max_tokens: int) -> dict:
        return {"num_predict": max_tokens, "num_ctx": self.ctx}

    def _payload(self, model: str, messages: list, tools: list,
                 max_tokens: int) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "options": self._options(max_tokens),
        }
        if tools:
            payload["tools"] = tools
        if self._think_known:
            payload["think"] = self.think
        if self._kwargs_known and not self.think:
            # Настоящий выключатель размышлений — вот этот, а не think.
            # think у Ollama означает «разбирать ли размышления отдельным
            # полем»; модель при нём думает ровно столько же. А гибридные
            # модели (Qwen3.5 и родня) слушаются именно enable_thinking из
            # аргументов шаблона чата. Для голоса это принципиально: предел
            # ответа у робота 384 токена, и думающая модель выбирает его
            # целиком на рассуждения, обрывая фразу на полуслове.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def chat(self, model: str, messages: list, tools: list, max_tokens: int):
        """Поток ответов Ollama, разобранный по строкам."""
        # Соединение открываем до первой выдачи: тогда отказ можно исправить и
        # повторить, не показав наружу половину ответа.
        resp = None
        # Попыток на одну больше, чем необязательных параметров: каждый отказ
        # гасит ровно один из них, и после последнего должен остаться заход,
        # который дойдёт до Ollama.
        for _ in range(3):
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
                низ = body.lower()
                if self._kwargs_known and "chat_template_kwargs" in низ:
                    log.warning("эта сборка Ollama не знает chat_template_kwargs "
                                "— размышления гибридных моделей выключить "
                                "нечем (%s)", body[:200])
                    self._kwargs_known = False
                    continue
                if self._think_known and "think" in низ:
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
        # num_ctx здесь тот же, что в бою, и это не мелочь: Ollama держит в
        # памяти модель ВМЕСТЕ с KV-кэшем нужного размера, и запрос с другим
        # num_ctx заставляет её перезагрузить всё заново. Прогрев с чужим
        # числом не экономил ничего — он просто грел не то.
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "привет"}],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "think": self.think,
            "options": self._options(1),
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
# Рассуждающие модели: почему они дороги именно голосу
# --------------------------------------------------------------------------
# Размышления модели наружу не идут — их режет фильтр по дороге к речи. Но
# ВРЕМЯ на них тратится полностью, и всё это время робот молчит. На живом
# роботе «Да, я здесь!» стоило 695 токенов вывода и девяти секунд, из которых
# восемь ушли на текст, который никто никогда не увидит.
#
# Выключить это у гибридной модели нечем. Перепробовано три способа, и все
# три записаны в этом файле как неудачи: /no_think модель не понимает и
# отвечает на него вслух; think=false у Ollama означает «не разбирай
# размышления», а не «не думай»; chat_template_kwargs слушается не каждой
# сборкой и не каждой моделью. Настоящее лечение одно — взять модель, которая
# не рассуждает по построению.
#
# Поэтому здесь не уговоры, а подстановка: если нужная сборка уже скачана,
# берём её молча. Если нет — говорим ровно одну команду, которую надо
# выполнить, и продолжаем работать как работали.
РАССУЖДАЮТ = ("deepseek-r1", "qwq", "magistral", "reasoning", "-thinking")

# Чем заменить рассуждающую модель. Ключ — начало имени, значение — сборка
# того же семейства и размера, обученная отвечать сразу.
БЕЗ_РАССУЖДЕНИЙ = {
    "qwen3:0.6b": "qwen3:0.6b-instruct",
    "qwen3:1.7b": "qwen3:1.7b-instruct",
    "qwen3:4b": "qwen3:4b-instruct-2507-q4_K_M",
    "qwen3:8b": "qwen3:8b-instruct",
    "qwen3:14b": "qwen3:14b-instruct",
}


def рассуждает(имя: str) -> bool:
    """Думает ли эта модель вслух по построению.

    Гибриды Qwen3 — да: у них размышления включены в шаблоне запроса. А вот
    сборки с «instruct» в имени — отдельный выпуск, который не рассуждает
    вовсе, и путать их нельзя: имя различается одним словом, а поведение
    противоположно.
    """
    низ = имя.lower()
    if "instruct" in низ or "no-think" in низ:
        return False
    if низ.startswith("qwen3") or низ.startswith("qwen3."):
        return True
    return any(с in низ for с in РАССУЖДАЮТ)


def _семейство(имя: str) -> str:
    """Имя без хвоста квантования: qwen3:4b-q4_K_M → qwen3:4b."""
    низ = имя.lower()
    if ":" not in низ:
        return низ
    основа, тег = низ.split(":", 1)
    return f"{основа}:{тег.split('-', 1)[0]}"


def выбрать_модель(нужна: str, скачаны: list[str]) -> tuple[str, str]:
    """Какую модель брать на самом деле и что сказать человеку.

    Возвращает (имя, что сказать). Пустая вторая строка — говорить нечего.

    Молча подменять можно только на уже скачанное: предложить несуществующую
    модель значит сломать разговор целиком ради скорости, а неотвечающий
    робот хуже медленного.
    """
    if not нужна or not рассуждает(нужна):
        return нужна, ""

    хочется = БЕЗ_РАССУЖДЕНИЙ.get(_семейство(нужна))
    # Подойдёт любая нерассуждающая сборка того же семейства и размера —
    # человек мог скачать её с другим квантованием, и заставлять его качать
    # ровно нашу было бы придирками.
    родня = [м for м in скачаны
             if _семейство(м) == _семейство(нужна) and not рассуждает(м)]
    if родня:
        # Из нескольких берём ту, что мы и советовали: остальные — на выбор
        # человека, но предпочтение должно быть предсказуемым.
        лучшая = next((м for м in родня if м == хочется), родня[0])
        return лучшая, (f"беру {лучшая} вместо {нужна}: та думает вслух перед "
                        f"каждым ответом, и это секунды молчания на каждой фразе")
    if not хочется:
        return нужна, (f"модель {нужна} думает вслух перед ответом — это "
                       f"секунды молчания на каждой фразе. Замены для неё я "
                       f"не знаю; ищите сборку с «instruct» в имени")
    return нужна, (f"модель {нужна} думает вслух перед каждым ответом, и "
                   f"выключить это нечем — это секунды молчания на каждой "
                   f"фразе. Лечится одной командой:  ollama pull {хочется}")


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

    def __init__(self, size: str, language: str = "ru",
                 wake: str = "Кузя") -> None:
        self.size = size
        self.language = language
        # Как зовут робота. Нужно распознаванию: короткое редкое имя оно
        # слышит хуже всего, а ошибиться в нём дороже всего.
        self.wake = wake
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

    # Подсказки модели (initial_prompt) здесь нет, и это не забывчивость.
    # Её уже пробовали и убрали, о чём написано в voice/robot_voice/stt.py:
    # на шумной или тихой записи Whisper начинает повторять слова из подсказки
    # («сантиметров, сантиметров, сантиметров…»), разгоняя генерацию до предела,
    # и секунда звука разбирается полминуты. Ровно этот почерк и виден в живом
    # логе: «Ууууу…» на двести знаков с высокой уверенностью.
    #
    # Имя робота вместо подсказки лечится моделью: русское дообучение
    # large-v3-turbo слышит «Кузя» верно, а small — нет.

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


def _текстом(ответ) -> str:
    """Распознанное — строкой, что бы ни вернула библиотека.

    GigaAM отдаёт не строку, а объект с полем text. Полагаться на одно имя
    поля не хочется: библиотека молодая, и в разных версиях это уже называлось
    по-разному. Поэтому спрашиваем по очереди и в самом конце соглашаемся на
    строковое представление — робот не должен глохнуть из-за переименованного
    поля.
    """
    if ответ is None:
        return ""
    if isinstance(ответ, str):
        return ответ.strip()
    for имя in ("text", "transcription", "transcript"):
        значение = getattr(ответ, имя, None)
        if isinstance(значение, str):
            return значение.strip()
    return str(ответ).strip()


class GigaAM:
    """Русское распознавание от Сбера. Тот же договор, что у Whisper.

    Зачем оно рядом с Whisper. Whisper многоязычен, и русский в нём — доля
    общего корпуса; GigaAM учили на семистах тысячах часов русской речи, и на
    коротких редких словах — вроде имени робота — это заметно. Ради этого он
    сюда и добавлен.

    Чем платим — сказать честно. GigaAM не отдаёт уверенность: у Whisper есть
    avg_logprob, по которому робот решает, можно ли по фразе ехать, а здесь
    такого числа нет. Значит с ним отключается защита «расслышал слишком
    плохо». Текстовые сетки — заученные концовки роликов и зацикливание —
    работают по-прежнему, а вот от «поехал не туда, потому что послышалось»
    остаётся только узнавание по голосу.

    Поэтому выбор остаётся за человеком: --stt gigaam включает, по умолчанию
    Whisper. И если GigaAM не поднимется, робот не оглохнет — возьмётся Whisper.
    """

    def __init__(self, name: str = "v3_e2e_rnnt") -> None:
        self.size = name
        self.device = "не загружена"
        self._model = None
        self._lock = threading.Lock()

    @staticmethod
    def ffmpeg_есть() -> bool:
        """Есть ли ffmpeg. Без него GigaAM не прочитает ни одного файла.

        Своего декодера у него нет: звук он читает, запуская ffmpeg. Проверять
        это надо при запуске, а не на первой фразе — иначе робот встречает
        человека трассировкой вместо ответа, и так на каждое слово.
        """
        return shutil.which("ffmpeg") is not None

    def _load(self):
        import gigaam
        import torch

        if not self.ffmpeg_есть():
            raise RuntimeError(
                "нет ffmpeg — GigaAM читает звук только через него. "
                "Windows: winget install Gyan.FFmpeg; Debian: apt install ffmpeg")
        # Видеокарта, если есть: на процессоре GigaAM тоже быстр, но зачем.
        куда = "cuda" if torch.cuda.is_available() else "cpu"
        модель = gigaam.load_model(self.size, device=куда)
        self.device = куда
        return модель

    def warm(self) -> None:
        started = time.monotonic()
        with self._lock:
            if self._model is None:
                self._model = self._load()
        log.info("gigaam: модель %s на %s", self.size, self.device)
        # Загрузить и запустить — на видеокарте разные вещи. Первый настоящий
        # прогон платит за всю ленивую подготовку разом: подгрузку библиотек
        # CUDA, создание контекста, подбор алгоритмов свёртки. У Conformer,
        # на котором построен GigaAM, свёрток много, и на живом ПК это стоило
        # ПЯТЬДЕСЯТ ПЯТЬ СЕКУНД — при том, что следующая фраза той же длины
        # разобралась за шесть десятых.
        #
        # Без холостого прогона эти секунды достаются первому, кто заговорит
        # с роботом. Он подумает, что робот сломался, и будет прав: минута
        # молчания в ответ на «привет» — это и есть сломанный робот.
        if self.device == "cuda":
            self.transcribe(_wav(b"\0" * (16000 * 2), 16000))
        log.info("распознавание готово, прогрев занял %.0f с",
                 time.monotonic() - started)

    def transcribe(self, wav: bytes) -> tuple[str, float | None]:
        import tempfile

        with self._lock:
            if self._model is None:
                self._model = self._load()
            started = time.monotonic()
            # GigaAM принимает путь к файлу, а не байты. Пишем во временный и
            # сразу убираем: на Windows открытый файл повторно не открыть,
            # поэтому закрываем до вызова.
            имя = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(wav)
                    имя = f.name
                text = _текстом(self._model.transcribe(имя))
            finally:
                if имя:
                    with contextlib.suppress(OSError):
                        os.unlink(имя)

        if made_up(text):
            log.info("выдумал концовку ролика (%r) — считаю тишиной", text)
            text = ""
        spent = time.monotonic() - started
        length = len(wav) / (16000 * 2)      # 16 кГц, моно, int16
        log.info("gigaam: %.2f с на %.1f с звука (×%.2f) | уверенности нет → %r",
                 spent, length, spent / length if length else 0, text)
        # Уверенности у GigaAM нет — так и говорим наверх, а не выдумываем
        # число: робот по нему решает, ехать ли, и ложная уверенность опаснее
        # честного «не знаю».
        return text, None


# --------------------------------------------------------------------------
# Кто говорит
# --------------------------------------------------------------------------
# Слова, которыми не зовут человека. Закрытый класс — вопросительные и
# местоимения, — поэтому перечислен целиком.
#
# Робот такую же проверку делает у себя, и это НЕ дублирование ради симметрии.
# Имя приходит сюда по сети, от сборки, которую здесь никто не выбирает: на
# живом роботе стоял старый образ, телевизор ответил на «как тебя зовут?»
# словом «Что» — и на ПК завёлся слепок ЖИЛЬЦА ПО ИМЕНИ «Что». В журнале:
# «узнаю по голосу 7: Игорь, голос 1, голос 3, Рома, голос 2, голос 4, Что».
# Он потом участвует в каждом сравнении и тянет на себя чужие фразы.
#
# Слепки живут на ПК и переживают любое обновление робота — значит и защищать
# их надо здесь. Робот чинит будущее, ПК — ещё и прошлое: см. _прибраться.
НЕ_ИМЯ = {
    "что", "кто", "где", "куда", "откуда", "когда", "как", "какой", "какая",
    "какое", "какие", "чей", "чья", "чьё", "чьи", "сколько", "почему",
    "зачем", "который", "которая",
    "я", "ты", "он", "она", "оно", "они", "мы", "вы",
    "меня", "тебя", "его", "её", "их", "нас", "вас",
    "мне", "тебе", "ему", "ей", "им", "нам", "вам",
    "мой", "моя", "моё", "мои", "твой", "твоя", "твоё", "твои",
    "наш", "наша", "ваш", "ваша", "свой", "себя",
    "это", "этот", "эта", "эти", "тот", "та", "те", "там", "тут", "здесь",
    "вот", "всё", "все", "весь", "вся",
    "ладно", "хорошо", "так", "ну", "значит", "кстати", "нет", "да",
    "конечно", "правда", "может", "стоп", "стой", "хватит", "тихо",
    "погоди", "подожди", "ага", "окей", "привет", "спасибо", "не",
}

def кличка(name: str) -> bool:
    """«голос 4» — кличка безымянного, а не имя. Проверку имени не проходит."""
    return name.startswith("голос ") and name[6:].isdigit()


def годится_в_имя(name: str) -> bool:
    """Можно ли завести слепок под таким именем."""
    name = (name or "").strip()
    if not name:
        return False
    if кличка(name):
        return True
    if not 2 <= len(name) <= 20:
        return False
    # Буквы и дефис, начинается с буквы. Цифры и знаки в имени человека — это
    # мусор распознавания, а не имя.
    if not name[0].isalpha() or not name.replace("-", "").isalpha():
        return False
    return name.lower() not in НЕ_ИМЯ


class Voiceprints:
    """Узнаёт человека по голосу.

    Работает не на словах, а на тембре: ECAPA-TDNN сворачивает любую фразу в
    вектор из двух сотен чисел, и у одного человека эти векторы лежат кучно, а
    у разных людей — врозь. Сравнение — косинус между векторами.

    Знакомства как обряда здесь НЕТ, и это главное решение. Первая версия
    просила «скажи три фразы» — на живом роботе это не сработало ни разу:
    Whisper ломал саму просьбу, человек сбивался, а под конец робот отвечал,
    что голоса запоминать не умеет. Да и по существу обряд лишний: люди не
    представляются пылесосу.

    Поэтому голос заводится сам. Каждая фраза, сказанная роботу, сравнивается
    с известными: похоже на кого-то — это он, и слепок уточняется; явно ни на
    кого — заводится новый, пока безымянный. Имя приходит потом, из разговора:
    «меня зовут Игорь» — и безымянный становится Игорем вместе со всем, что
    робот уже успел о нём записать.

    Учимся ТОЛЬКО на обращённой к роботу речи. Это не мелочь: телевизор в
    комнате говорит больше всех, и если учиться на всём подряд, слепки
    расползутся по дикторам, а однажды чужой голос подмешается в хозяйский.
    Поэтому /stt только узнаёт, а запоминает отдельный вызов — робот делает
    его, когда убедился, что говорили с ним.

    Слепки лежат здесь, на ПК, — там же, где считаются. Личные дела живут на
    роботе: они нужны ему для разговора и тогда, когда ПК выключен.
    """

    # Косинус между слепками. У ECAPA свой человек обычно даёт 0.7 и выше,
    # чужой — 0.3 и ниже. Настоящее число подберём по живому логу: похожесть
    # пишется в каждый ответ ровно ради этого.
    #
    # Порогов два, и они несимметричны. SAME — «это точно он»: только выше
    # него мы приписываем фразу человеку и уточняем его слепок. NEW — «это
    # точно не он»: только НИЖЕ него заводим нового. Между ними — молчание:
    # не узнали и не запомнили. Ошибиться в сторону «не узнал» дёшево, а
    # слить двух людей в одного — значит показать одному записи про другого.
    # Числа с живого робота: свой же голос против своего слепка дал 0.50–0.52,
    # а не обещанные 0.7. Микрофон телефона, комната, короткие фразы — и порог
    # 0.62 не срабатывал НИ РАЗУ: за пять минут разговора Игорь развалился на
    # четыре разных «голоса». Опущено по факту, а не по описанию модели.
    SAME = 0.45
    NEW = 0.30

    # Сколько голосов держим. Больше в квартире не живёт, а лишние — это
    # телевизор и гости на один вечер.
    LIMIT = 12

    # Короче этого фразу для завода нового голоса не берём. Узнать по «ага»
    # ещё можно, а вот заводить по нему нового человека — верный способ
    # расплодить призраков. Полторы секунды оказалось мало: за вечер разговора
    # Игорь всё равно развалился на «Игорь, голос 1, голос 2, голос 3».
    ENOUGH = 2.0

    # Сколько раз ПОДРЯД надо услышать незнакомца, чтобы завести ему слепок,
    # и насколько эти разы должны быть похожи друг на друга.
    #
    # Одна фраза для этого решения — слишком мало. Голос уплывает: человек
    # отвернулся, сказал тише, простыл, микрофон телефона подавился — и
    # похожесть на собственный слепок падает ниже NEW. Раньше этого хватало,
    # чтобы завести нового человека, и хозяин дома размножался кличками, а
    # робот при следующей фразе узнавал его уже как чужого.
    #
    # Двух подряд достаточно: случайный провал так не повторяется, а
    # настоящий новый человек говорит не одной фразой. Требуем ещё и чтобы обе
    # фразы были похожи между собой, иначе двое чужих подряд (гости в комнате)
    # склеились бы в одного.
    ПОДРЯД = 2

    # Насколько далеко назад помнит слепок. Раньше усреднение было по ВСЕМ
    # фразам сразу: вектор умножался на их число, прибавлялась новая и делилось
    # на число плюс один. Среднее при этом честное, но вес новой фразы падает
    # без предела — на живом роботе в слепке Игоря накопилось 162 фразы, и
    # каждая следующая весила уже шесть десятых процента. То есть слепок
    # застывал навсегда.
    #
    # Плохо это не теоретически. Голос меняется вместе с обстановкой: сменили
    # микрофон телефона на браузерный, переставили робота в другую комнату,
    # человек простыл — и застывший слепок перестаёт узнавать хозяина, а
    # починить это нечем, кроме как забыть голос и завести заново.
    #
    # Сорок фраз — это вес около двух с половиной процентов у новой: случайный
    # кашель слепок не портит, а переезд в другую комнату он переживёт за
    # пару десятков фраз. Счётчик фраз при этом продолжает расти: он показывает
    # человеку, сколько робот наслушался, и на усреднение больше не влияет.
    ПАМЯТЬ = 40

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
        # Слепки последних фраз: /stt их только считает, а запоминает отдельный
        # вызов — когда робот убедился, что говорили с ним.
        self._recent: dict[str, tuple] = {}
        # Незнакомец, услышанный подряд: слепок и сколько раз. См. ПОДРЯД.
        self._чужак = None
        self._чужака_раз = 0
        self._read()

    # --- хранение --------------------------------------------------------
    def _read(self) -> None:
        try:
            self.people = json.loads(self.store.read_text("utf-8"))
        except (OSError, ValueError):
            self.people = {}
        self._прибраться()

    def _прибраться(self) -> None:
        """Выносит слепки, заведённые под тем, что именем не является.

        Чинить только будущее здесь мало. Слепки лежат на ПК и переживают
        любое обновление робота: жилец «Что», заведённый телевизором один раз,
        так и останется в списке и будет участвовать в каждом сравнении.
        Поэтому проверка стоит и на входе — при чтении файла.

        Слепок не сливаем ни с кем: чьи в нём фразы, неизвестно, а приписать
        их живому человеку — то же самое, что показать одному записи другого.
        """
        мусор = [n for n in self.people if not годится_в_имя(n)]
        for n in мусор:
            log.warning("слепок %r заведён не под именем — выношу "
                        "(так телевизор однажды завёл жильца)", n)
            del self.people[n]
        if мусор:
            self._write()

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
    def identify(self, wav: bytes, seconds: float = 0.0) -> tuple[str, float, str]:
        """Кто это сказал, насколько похоже и метка для последующего запоминания.

        Ничего не меняет: узнать надо на каждой фразе, а запоминать — только
        то, что сказали роботу. Метка позволяет вернуться к этому слепку, не
        пересылая звук второй раз.
        """
        try:
            mine = self._vector(wav)
        except Exception as e:
            log.warning("не смог снять слепок голоса (%s)", e)
            return "", 0.0, ""
        if mine is None:
            return "", 0.0, ""

        best, score = self._nearest(mine)
        # Пишем расклад целиком: без живых чисел пороги подбираются гаданием,
        # а гадать тут дорого — на кону чужие записи, показанные не тому.
        if self.people:
            log.info("голоса: %s", ", ".join(
                f"{n} {self._one(mine, n):.2f}" for n in sorted(self.people)))
        метка = f"{int(time.time() * 1000):x}"
        self._recent[метка] = (mine, seconds)
        # Держим только последние: робот подтверждает сразу за распознаванием,
        # а копить чужие векторы в памяти незачем.
        for старая in list(self._recent)[:-8]:
            self._recent.pop(старая, None)
        if score < self.SAME:
            return "", score, метка
        return best, score, метка

    def _one(self, mine, name: str) -> float:
        import numpy as np

        return float(np.dot(mine, np.asarray(self.people[name]["вектор"],
                                             dtype="float32")))

    def _nearest(self, mine) -> tuple[str, float]:
        best, score = "", -1.0
        for name in self.people:
            near = self._one(mine, name)
            if near > score:
                best, score = name, near
        return best, score

    def _merge_twins(self) -> dict[str, str]:
        """Сливает слепки, оказавшиеся одним человеком.

        Пороги строги намеренно, и расплата за это — расщепление: один человек
        в разных настроениях заводится как «голос 1» и «голос 4». По одной
        фразе этого не видно, а по накопленным слепкам — прекрасно видно, они
        похожи друг на друга сильнее порога. Чиним задним числом: имя
        побеждает кличку, обкатанный слепок — свежий.
        """
        import numpy as np

        куда: dict[str, str] = {}
        while True:
            пара = None
            имена = list(self.people)
            for i, a in enumerate(имена):
                va = np.asarray(self.people[a]["вектор"], dtype="float32")
                for b in имена[i + 1:]:
                    vb = np.asarray(self.people[b]["вектор"], dtype="float32")
                    if float(np.dot(va, vb)) >= self.SAME:
                        пара = (a, b)
                        break
                if пара:
                    break
            if not пара:
                return куда
            a, b = пара
            # Кличка уступает имени; при прочих равных — тот, у кого фраз больше.
            если_кличка = (a.startswith("голос "), -self.people[a].get("фраз", 0))
            если_вторая = (b.startswith("голос "), -self.people[b].get("фраз", 0))
            главный, лишний = (a, b) if если_кличка <= если_вторая else (b, a)
            log.info("голоса %r и %r — один человек, сливаю в %r",
                     a, b, главный)
            ушедший = self.people.pop(лишний)
            карта = self.people[главный]
            всего = карта.get("фраз", 0) + ушедший.get("фраз", 0)
            смесь = (np.asarray(карта["вектор"], dtype="float32") * карта.get("фраз", 0)
                     + np.asarray(ушедший["вектор"], dtype="float32")
                     * ушедший.get("фраз", 0))
            смесь = смесь / (float(np.linalg.norm(смесь)) or 1.0)
            self.people[главный] = {"вектор": [float(x) for x in смесь],
                                    "фраз": всего, "слит_из": лишний}
            # Куда переехал каждый слитый — включая тех, кто переехал раньше в
            # того, кто сам только что переехал.
            куда[лишний] = главный
            for откуда, куда_шёл in list(куда.items()):
                if куда_шёл == лишний:
                    куда[откуда] = главный

    def confirm(self, метка: str, name: str = "") -> str:
        """Запоминает голос последней фразы. Возвращает, за кем он записан.

        Зовётся только когда робот убедился, что говорили с ним. Три исхода:
        узнали — уточняем слепок; явно никто из известных — заводим нового,
        пока безымянного; между порогами — молчим и не портим ничего.
        """
        сохранённое = self._recent.pop(метка, None)
        if сохранённое is None:
            return ""
        mine, seconds = сохранённое
        best, score = self._nearest(mine)

        if name and not годится_в_имя(name):
            # Имя пришло по сети, от сборки, которую здесь никто не выбирает.
            # Робот такое отсекает у себя, но старый образ — нет, а слепок
            # заводится и остаётся навсегда именно тут. Имя выбрасываем, голос
            # обрабатываем дальше как безымянный: человек-то говорил.
            log.warning("именем %r голос не заведу — это не имя", name)
            name = ""

        if name:
            # Имя пришло из разговора. Если этот голос уже ходит под кличкой,
            # переименовываем вместе со всей историей: человек не должен
            # терять слепок из-за того, что представился поздно.
            if best and score >= self.SAME and best != name:
                self._rename(best, name)
            return self._absorb(name, mine)
        if score >= self.SAME:
            self._чужак, self._чужака_раз = None, 0
            return self._absorb(best, mine)
        if score < self.NEW and seconds >= self.ENOUGH:
            return self._может_новый(mine)
        # Серая зона: похоже, но не точно. Не приписываем и не заводим.
        # Счётчик незнакомца тоже сбрасываем: «подряд» значит подряд.
        self._чужак, self._чужака_раз = None, 0
        return ""

    def _может_новый(self, mine) -> str:
        """Заводит слепок незнакомцу, но только услышав его ПОДРЯД. См. ПОДРЯД."""
        import numpy as np

        тот_же = (self._чужак is not None
                  and float(np.dot(mine, self._чужак)) >= self.SAME)
        if тот_же:
            self._чужака_раз += 1
        else:
            self._чужак, self._чужака_раз = mine, 1
        if self._чужака_раз < self.ПОДРЯД:
            log.info("незнакомый голос (%d из %d) — подожду ещё фразу, "
                     "прежде чем заводить нового",
                     self._чужака_раз, self.ПОДРЯД)
            return ""
        # Заводим по накопленному, а не по последней фразе: среднее из двух
        # устойчивее одной, а именно по этому слепку человека будут узнавать.
        смесь = (np.asarray(self._чужак, dtype="float32") + mine) / 2
        смесь = смесь / (float(np.linalg.norm(смесь)) or 1.0)
        self._чужак, self._чужака_раз = None, 0
        return self._absorb(self._new_name(), смесь)

    def _absorb(self, name: str, mine) -> str:
        """Вливает фразу в слепок человека, уточняя его."""
        import numpy as np

        card = self.people.get(name) or {"вектор": [0.0] * len(mine), "фраз": 0}
        # Скользящее среднее: каждая новая фраза уточняет слепок, а не
        # заменяет его. Один зевок или кашель тогда не портит всё.
        #
        # Вес прошлого ограничен ПАМЯТЬю. Без ограничения слепок застывает:
        # после полутора сотен фраз новая весит меньше процента, и голос,
        # изменившийся вместе с микрофоном или комнатой, узнаваться перестаёт
        # навсегда.
        вес = min(int(card["фраз"]), self.ПАМЯТЬ)
        было = np.asarray(card["вектор"], dtype="float32") * вес
        средний = (было + mine) / (вес + 1)
        средний = средний / (float(np.linalg.norm(средний)) or 1.0)
        self.people[name] = {"вектор": [float(x) for x in средний],
                             "фраз": card["фраз"] + 1}
        # Слияние может увести этот самый слепок в другой: имя побеждает
        # кличку, и «голос 4» растворяется в «голосе 1». Дальше говорить надо
        # про того, кто уцелел, — иначе падение с KeyError ровно там, где
        # робот только что успешно всех узнал.
        name = self._merge_twins().get(name, name)
        self._trim()
        self._write()
        # Уборка тоже могла выбросить этот слепок, если он оказался самым
        # нехоженым. Тогда честнее промолчать, чем врать про запомненное.
        if name not in self.people:
            log.info("голос не удержался в памяти: слишком мало фраз")
            return ""
        log.info("голос %s уточнён, фраз в слепке: %d",
                 name, self.people[name]["фраз"])
        return name

    def _new_name(self) -> str:
        n = 1
        while f"голос {n}" in self.people:
            n += 1
        log.info("новый голос: голос %d", n)
        return f"голос {n}"

    def _rename(self, old: str, new: str) -> None:
        if old == new or old not in self.people:
            return
        card = self.people.pop(old)
        # Если под этим именем уже кто-то есть, побеждает более обкатанный
        # слепок: у него больше фраз, значит он вернее.
        было = self.people.get(new)
        if было is None or было.get("фраз", 0) < card.get("фраз", 0):
            self.people[new] = card
        log.info("голос %r теперь %r", old, new)

    def _trim(self) -> None:
        """Держим не больше LIMIT голосов, выбрасывая самые нехоженые.

        Безымянные уходят первыми: «голос 4», услышанный дважды, — это почти
        наверняка телевизор или гость на один вечер, а Игорь с сотней фраз
        должен пережить любую уборку.
        """
        while len(self.people) > self.LIMIT:
            кого = min(self.people,
                       key=lambda n: (not n.startswith("голос "),
                                      self.people[n].get("фраз", 0)))
            log.info("забываю голос %r: слишком мало фраз", кого)
            del self.people[кого]

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
        # Ограничение потоков — НЕ настройка этого модуля, а настройка всего
        # процесса: torch.set_num_threads действует на любые вычисления на
        # процессоре, включая распознавание речи и узнавание голоса, которые
        # живут рядом. Половина ядер синтезу нужна ровно тогда, когда
        # распознавание тоже считается процессором и они дерутся.
        #
        # А когда видеокарта видна, распознавание уезжает на неё (см. GigaAM
        # ._load), драка кончается, и урезать ядра больше некому и незачем:
        # узнавание голоса остаётся на процессоре и работает вдвое быстрее.
        if not torch.cuda.is_available():
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

    def кто(self, speaker: str = "") -> str:
        """Каким голосом на самом деле прозвучит эта фраза.

        Робот называет этим именем свой кэш готовых фраз. Без него он называл
        кэш тем, что записано в его собственных настройках, — а голос
        выбирается здесь, ключом --voice. Стоило поменять голос на ПК, и робот
        продолжал доставать частые заготовки прежним голосом: половина фраз
        звучала одним голосом, половина другим.
        """
        return speaker if speaker in self.VOICES else self.speaker

    def say(self, text: str, speaker: str = "") -> bytes:
        """Синтезирует фразу и отдаёт её готовым wav-файлом."""
        import numpy as np

        with self._lock:
            if self._model is None:
                self._model = self._load()
                self.ready = True
            started = time.monotonic()
            who = self.кто(speaker)
            audio = self._model.apply_tts(text=text, speaker=who,
                                          sample_rate=self.RATE)

        raw = (np.asarray(audio, dtype="float32").clip(-1.0, 1.0) * 32767
               ).astype("<i2").tobytes()
        spent = time.monotonic() - started
        length = len(raw) / 2 / self.RATE
        log.info("голос: %.2f с на %.1f с речи (×%.2f) → %r",
                 spent, length, spent / length if length else 0, text[:60])
        return _wav(raw, self.RATE)


def _wav_seconds(wav: bytes) -> float:
    """Длина записи в секундах. Нужна, чтобы не заводить голос по «ага»."""
    import io
    import wave as wavelib

    try:
        with wavelib.open(io.BytesIO(wav)) as w:
            return w.getnframes() / (w.getframerate() or 1)
    except Exception:
        return 0.0


def _wav(raw: bytes, rate: int) -> bytes:
    """Оборачивает сырой звук в wav-заголовок. Без внешних библиотек."""
    import struct

    header = b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(raw)) + raw


# --------------------------------------------------------------------------
# Аватар: та же логика позы, что и на роботе, — просто выполненная здесь
# --------------------------------------------------------------------------
# face/character.py ничего не знает про pygame (сама Кисть — в face/face.py),
# поэтому его можно позвать и отсюда, без единой лишней зависимости. Решение
# «что делать» остаётся ОДНО на двоих: и у робота на экране, и в браузере на
# ПК танец начинается от одного и того же условия, посчитанного один раз.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
import character  # noqa: E402
import director  # noqa: E402
import scenes  # noqa: E402

# Мост к Open-LLM-VTuber — опциональная надстройка, см. pc/ollv_bridge.py.
# Отдельный модуль рядом, не пакет: включается только переменной окружения
# OLLV_URL, и без неё этого импорта как будто и не было.
import ollv_bridge  # noqa: E402


def свои_праздники(папка) -> dict:
    """«праздники» из config.local.json: {"ДД-ММ": "что сказать"}; нет — пусто."""
    try:
        своё = json.loads((папка / "config.local.json").read_text(encoding="utf-8"))
        праздники = своё.get("праздники") or {}
        return {str(к): str(з) for к, з in праздники.items()} if isinstance(праздники, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def страница_аватара(папка=None) -> str:
    """Какую страницу аватара снимать: «» (Live2D, index.html) или «3d.html».

    Выбор в config.local.json: {"движок": "3d"}. Незнакомое слово — Live2D:
    он не требует ничего, кроме модели, а 3D без файла .vrm покажет только
    объяснение, где его взять. Молча остаться без персонажа из-за опечатки в
    настройке — худший исход, чем показать не тот движок.
    """
    папка = папка if папка is not None else Handler.АВАТАР_ПАПКА
    try:
        своё = json.loads((папка / "config.local.json").read_text(encoding="utf-8"))
        движок = str(своё.get("движок", "")).strip().lower()
    except (OSError, ValueError, AttributeError):
        движок = ""
    return "3d.html" if движок in ("3d", "vrm", "3д") else ""


def модель_настроена(папка=None) -> tuple[bool, str]:
    """Задана ли модель для выбранного движка — раньше, чем открывать браузер.

    Живая поломка: `config.json` по умолчанию указывал на образец Shizuku
    (аниме-девушка из samples/), и КАЖДЫЙ, кто просто запускал kuzya_pc.py
    без единой строчки своих настроек, получал на экране робота не Кузю, а
    её — молча, без единого предупреждения. Теперь модель по умолчанию
    пустая, а это не ошибка: значит, персонажа рисует сам робот (домовёнок,
    `face/face.py`), как и было исходно. Съёмку headless-браузером стоит
    вообще не запускать, пока человек явно не вписал путь к модели в
    `config.local.json` — иначе при опечатке или снесённом файле экран
    робота получит не пустоту, а вшитый в кадр текст ошибки страницы.
    """
    папка = папка if папка is not None else Handler.АВАТАР_ПАПКА
    поле = "модель3d" if страница_аватара(папка) == "3d.html" else "модель"
    try:
        общее = json.loads((папка / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        общее = {}
    путь = общее.get(поле, "")
    местный = папка / "config.local.json"
    if местный.is_file():
        try:
            своё = json.loads(местный.read_text(encoding="utf-8"))
            if поле in своё:
                путь = своё[поле]
        except (OSError, ValueError):
            pass
    путь = str(путь or "").strip()
    if not путь:
        return False, ""
    цель = (папка / путь).resolve()
    try:
        цель.relative_to(папка.resolve())
    except ValueError:
        return False, путь
    return цель.is_file(), путь


def настроение_по_слову(ответ: str) -> str:
    """Вердикт модели → «рад» | «огорчён» | «спокоен» | «» (не разобрала).

    Модель просят ответить одним словом, но она может ответить фразой,
    в другом падеже или с точкой: ищем корень, а не сравниваем строки.
    """
    низ = (ответ or "").lower().replace("ё", "е")
    if "огорч" in низ or "груст" in низ or "печал" in низ:
        return "огорчён"
    if "рад" in низ or "весел" in низ:
        return "рад"
    # «споко», а не «спокой»: «спокоен» — самое вероятное слово — корня
    # «спокой» не содержит, и с ним вердикт модели пропадал.
    if "споко" in низ or "нейтр" in низ:
        return "спокоен"
    return ""


ВОПРОС_О_НАСТРОЕНИИ = ("Каким настроением звучит эта реплика робота? Ответь одним "
                       "словом: рад, огорчён или спокоен.\n\nРеплика: ")


def настроение_по_тексту(текст: str) -> str:
    """«рад» / «огорчён» / «» — по тому, ЧТО ответила модель.

    Робот сам таких эмоций не ставит: у лица есть рад() и огорчён(), но
    ни разговор, ни навыки их не зовут — модель отвечает текстом, и текст
    единственное, по чему можно судить о настроении ответа. Здесь — грубая
    прикидка по словам, не разбор смысла: «к сожалению, не могу» — огорчён,
    «отлично, сделано!» — рад. Ошибётся — персонаж на секунды сделает не то
    лицо; промолчит — лицо останется спокойным. Обе цены малы.
    """
    низ = (текст or "").lower().replace("ё", "е")
    if not низ:
        return ""
    грустное = ("к сожалению", "не могу", "не получ", "не смог", "не удал",
                "извини", "прости", "жаль", "увы", "не знаю", "ошибк",
                "не вышло", "не работает", "грустно", "не найд")
    радостное = ("отлично", "здорово", "ура", "с удовольствием", "прекрасно",
                 "класс", "супер", "поздравля", "готово", "сделал", "сделано",
                 "конечно", "рад ", "рада ", "молодец", "замечательно",
                 "люблю", "обожаю", "привет")
    if any(с in низ for с in грустное):
        return "огорчён"
    if any(с in низ for с in радостное) or "!" in низ:
        return "рад"
    return ""


# Рот по звуку — общий с роботом: face/lips.py (Губы). Здесь его кормит
# /tts, на роботе — то, что он играет сам (кэш, свой синтез).
from lips import Губы  # noqa: E402

class Аватар:
    """Держит последнее состояние робота и позу, посчитанную по нему.

    Раздельно намеренно: `_raw` — то, что прислал робот (эмоция, погода,
    название трека — то, что странице может пригодиться как есть), `_поза`
    — то, что решил Питомец (рот, наклон, метка). Страница в браузере не
    обязана знать про приоритет состояний и таймеры блуждания: это уже
    решено здесь, ей остаётся только нарисовать.
    """

    # Старше этого — робот молчит, и аватар обязан уснуть, а не застыть с
    # последним живым лицом. То же правило, что у face.json на роботе (там
    # секунда); здесь длиннее, потому что между ними сеть, и одна потерянная
    # посылка из десяти в секунду — не повод хлопать глазами.
    СТАРЕЕТ = 3.0

    def __init__(self, часы=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._часы = часы
        self._raw: dict = {}
        self._поза: dict = {}
        self._когда = float("-inf")
        # Числа для Питомца тут ничего не значат для портретной Live2D-модели
        # (она не бродит по экрану) — они нужны только форме конструктора;
        # умолчания в character.py рассчитаны на реальный экран, а не на это.
        self._питомец = character.Питомец(1280, 800, тело_ширина=0.34,
                                          шаг_длина=0.05)
        self._начало = часы()
        # Съёмка страницы для экрана робота. None — не поднята (нет
        # Playwright или выключена ключом), тогда /avatar/stream честно
        # отвечает 503, а робот рисует лицо сам.
        self.съёмка: Съёмка | None = None
        # Своё, чего робот не присылает: рот по звуку и настроение ответа.
        self.губы = Губы(часы)
        self._настроение = ""
        self._настроение_до = 0.0
        # Жизнь в покое: сценки, которые персонаж разыгрывает сам — по поводу
        # и без (face/scenes.py). Час и минута — для «пробило час» и утра.
        self.сценарист = scenes.Сценарист()
        self._часы_дня = time.localtime
        # Режиссёр (face/director.py): раз в полминуты в покое спрашивает
        # модель, чем человечку заняться, и кладёт её план сценаристу.
        # Спрашивает — kuzya_pc.main через спросить_режиссёра, в своём
        # потоке; пока идёт разговор с человеком, режиссёр в очередь к
        # модели не лезет (разговоров > 0), а сам вопрос — короткий.
        self.режиссёр = director.Режиссёр()
        self._режиссёр_думает = False
        self._разговоров = 0

    # Сколько держать настроение ответа: пока робот его произносит, плюс
    # немного. Скорость речи — около четырнадцати знаков в секунду.
    НАСТРОЕНИЕ_МИНИМУМ = 4.0
    ЗНАКОВ_В_СЕКУНДУ = 14.0

    # Кого спросить о плане сценок: (текст вопроса) -> текст ответа. Ставит
    # main — та же Ollama, что ведёт разговор. None — режиссёра нет, живём
    # жребием сценариста, как раньше.
    спросить_режиссёра = None

    def разговор_начался(self) -> None:
        with self._lock:
            self._разговоров += 1

    def разговор_кончился(self) -> None:
        with self._lock:
            self._разговоров = max(0, self._разговоров - 1)

    def _позвать_режиссёра(self, с: dict, t: float, м) -> None:
        """Под замком. Пора — собираем вопрос здесь, спрашиваем в потоке."""
        if (self.спросить_режиссёра is None or self._режиссёр_думает
                or self._разговоров > 0 or self._часы() < self._настроение_до):
            return
        if not self.режиссёр.пора(с, self.сценарист, t):
            return
        годные = self.сценарист.годные(с, t, м.tm_hour)
        вопрос = self.режиссёр.запрос(с, self.сценарист, t, м.tm_hour, м.tm_min)
        if not вопрос:
            return
        self._режиссёр_думает = True
        threading.Thread(target=self._спросить_режиссёра, args=(вопрос, годные),
                         name="режиссёр", daemon=True).start()

    def _спросить_режиссёра(self, вопрос: str, годные: list) -> None:
        try:
            ответ = self.спросить_режиссёра(вопрос)
        except Exception as e:                       # noqa: BLE001
            log.debug("режиссёр: модель не ответила (%s)", e)
            return
        finally:
            with self._lock:
                self._режиссёр_думает = False
        with self._lock:
            принято, реплика = self.режиссёр.принять(self.сценарист, ответ, годные)
        if принято:
            log.info("режиссёр: %s%s", ", ".join(принято),
                     f" — «{реплика}»" if реплика else "")
        else:
            log.info("режиссёр: ничего годного не выбрал (%s)",
                     " ".join(str(ответ).split())[:120])

    def принять(self, данные: dict) -> None:
        with self._lock:
            self._raw = данные if isinstance(данные, dict) else {}
            self._когда = self._часы()
            try:
                self._поза = self._питомец.кадр(
                    self._raw, self._часы() - self._начало)
            except Exception:                       # noqa: BLE001
                log.exception("аватар: не разобрал состояние робота")

    def озвучил(self, pcm: bytes, rate: int) -> None:
        """Синтез отдал звук роботу — рот теперь пойдёт по нему."""
        self.губы.добавить(pcm, rate)

    # Кого спросить о настроении ответа: (текст) -> «рад» | «огорчён» |
    # «спокоен» | «». Ставит main — это Ollama с той же моделью. Спрашиваем
    # в своём потоке: ответ модели идёт в динамик секунды, и вердикт через
    # полсекунды успевает; слова — сразу, как запас на случай отказа.
    спросить_настроение = None
    НАСТРОЕНИЕ_ЖДАТЬ = 4.0

    def ответил(self, текст: str) -> None:
        """Модель закончила ответ — настроение по тексту, на время речи."""
        настроение = настроение_по_тексту(текст)
        with self._lock:
            self._настроение = настроение
            self._настроение_до = self._часы() + max(
                self.НАСТРОЕНИЕ_МИНИМУМ, len(текст) / self.ЗНАКОВ_В_СЕКУНДУ + 2.0)
            срок = self._настроение_до
        if настроение:
            log.info("аватар: ответ звучит как «%s» (по словам)", настроение)
        if self.спросить_настроение is not None and текст.strip():
            threading.Thread(target=self._уточнить_настроение, args=(текст, срок),
                             name="настроение", daemon=True).start()

    def _уточнить_настроение(self, текст: str, срок: float) -> None:
        try:
            вердикт = настроение_по_слову(self.спросить_настроение(текст))
        except Exception as e:                       # noqa: BLE001
            log.debug("аватар: модель не оценила настроение (%s)", e)
            return
        with self._lock:
            if self._настроение_до != срок:
                return                            # уже другой ответ
            if вердикт == "спокоен":
                self._настроение = ""
            elif вердикт:
                self._настроение = вердикт
        if вердикт:
            log.info("аватар: модель говорит — «%s»", вердикт)

    def состояние(self) -> dict:
        with self._lock:
            сейчас = self._часы()
            if сейчас - self._когда > self.СТАРЕЕТ:
                # Батарею оставляем: она и на роботе переживает сон.
                #
                # А ВОТ ВЗГЛЯД И НАКЛОН ОБНУЛЯЕМ. Поза здесь замирает такой,
                # какой пришла последней, — и персонаж засыпал с головой,
                # вывернутой туда, где человек стоял три секунды назад: глаза
                # закрыты, а шея скручена. Связь с роботом рвётся обыденно
                # (после неудачного POST'а робот молчит пять секунд, а стареет
                # поза за три), так что видно это было постоянно.
                return {"эмоция": "сплю", "батарея": self._raw.get("батарея"),
                        "поза": {**self._поза, "метка": "спит", "рот": 0.0,
                                 "взгляд": 0.0, "наклон": 0.0}}
            с = {**self._raw, "поза": dict(self._поза)}
            # Рот — по звуку, если он есть; иначе остаётся синус Питомца.
            # Только пока робот САМ говорит, что говорит: перебили — рот
            # закрывается вместе с динамиком, а не доигрывает огибающую.
            # Свой звук (/tts) точнее по времени; нет своего — берём рот,
            # который робот считает по тому, что играет сам (кэш, piper).
            if с.get("говорит"):
                по_звуку = self.губы.рот()
                if по_звуку is None and isinstance(с.get("рот"), (int, float)):
                    по_звуку = float(с["рот"])
                if по_звуку is not None:
                    с["поза"]["рот"] = по_звуку
                    с["поза"]["рот_по"] = "звуку"
            # Настроение ответа — только поверх спокойного лица: тревогу,
            # непонимание и прочее, что робот ставит сам, оно не перебивает —
            # тот же порядок, что у эмоций на самом роботе.
            if (self._настроение and сейчас < self._настроение_до
                    and с.get("эмоция", "спокоен") == "спокоен"):
                с["эмоция"] = self._настроение
            # Сценка — по уже собранному состоянию (с настроением ответа и
            # ртом по звуку): она видит то же, что и страница.
            м = self._часы_дня()
            try:
                с["сцена"] = self.сценарист.кадр(с, сейчас - self._начало,
                                                 час=м.tm_hour, минута=м.tm_min,
                                                 день=м.tm_mday, месяц=м.tm_mon,
                                                 день_недели=м.tm_wday)
            except Exception:                       # noqa: BLE001
                log.exception("аватар: сценка не разыгралась")
                с["сцена"] = {"имя": "", "текст": "", "параметры": {}}
            self._считать_сценку(с["сцена"].get("имя") or "", сейчас)
            # Режиссёр — по той же обстановке, что видит сценарист. Любая
            # его беда — в лог, а не в кадр: кадр важнее плана.
            try:
                self._позвать_режиссёра(с, сейчас - self._начало, м)
            except Exception:                       # noqa: BLE001
                log.exception("режиссёр: не собрал вопрос")
            return с

    # Диагностика жизни в покое: без неё «человечек ничего не делает» —
    # немая беда. Сценарист молчит не по ошибке (исключение и так летит в
    # лог строкой выше) и не по расписанию (между сценками паузы 3–9 с,
    # каждая идёт 1–8 с — большую часть времени персонаж и должен просто
    # стоять), а третий вариант — сценарист правда не выбирает ничего —
    # снаружи не отличить от первых двух без счётчика. Раз в минуту в лог:
    # сколько сценок сыграно и какая идёт сейчас.
    _сцен_с = None
    _сцен_имя_было = ""
    _сцен_сыграно = 0
    ДИАГНОСТИКА_РАЗ_В = 60.0

    def _считать_сценку(self, имя: str, сейчас: float) -> None:
        if self._сцен_с is None:
            self._сцен_с = сейчас
        if имя and имя != self._сцен_имя_было:
            self._сцен_сыграно += 1
        self._сцен_имя_было = имя
        if сейчас - self._сцен_с >= self.ДИАГНОСТИКА_РАЗ_В:
            log.info("аватар: сценок за %.0f с — %d, сейчас «%s»",
                     сейчас - self._сцен_с, self._сцен_сыграно, имя or "(нет)")
            self._сцен_с, self._сцен_сыграно = сейчас, 0


class Съёмка:
    """Кадры страницы аватара — для экрана робота.

    Робот показать Live2D сам не может: у него нет видеокарты, а модель
    рисуется WebGL. Зато ПК может нарисовать её у себя — в headless-браузере,
    которого никто не видит, — и отдать роботу уже готовые картинки. Это
    единственный путь, которым та картинка вообще попадает на экран робота.

    Снимаем не скриншотами по таймеру (каждый — отдельная просьба к браузеру,
    сотня миллисекунд, и кадры выходят рваные), а штатным скринкастом
    Chrome (CDP Page.startScreencast): браузер сам отдаёт JPEG на каждый
    свой кадр, а мы только складываем последний.

    Браузер живёт ТОЛЬКО ПОКА ЕСТЬ СПРОС: робот тянет /avatar/stream —
    снимаем; отключился — через ПРОСТОЙ закрываем всё. Держать headless-
    браузер ради выключенного робота — это ядро процессора впустую.

    Хранилище кадров отделено от браузера намеренно: `положить()` умеет
    звать кто угодно, и самопроверка кладёт сюда свои картинки без единого
    браузера — проверяя поток по HTTP, а не сам Chrome.
    """

    ПРОСТОЙ = 20.0          # секунд без спроса — браузер закрываем
    ПОВТОР = 60.0           # секунд до новой попытки после неудачи с браузером
    КАЧЕСТВО = 60           # JPEG; при 1280×800 это ~30–60 КБ на кадр
    ТИШИНА = 1.0            # секунд без кадра от скринкаста — снимаем скриншотом
    # Кадр старше этого ПЕРВЫМ в поток не отдаём. Пока браузер жив, кадры
    # идут не реже раза в ТИШИНА; старше — значит, браузер уже умер или
    # закрыт, а кадр остался с прошлой жизни. Отдать его — значит показать
    # роботу застывший кадр как живой поток (см. _съёмка).
    СВЕЖИЙ = 2.0

    def __init__(self, адрес: str = "", размер: tuple[int, int] = (1280, 800),
                 кадров_в_секунду: int = 15, часы=time.monotonic) -> None:
        self.адрес = адрес
        self.размер = размер
        self.кадров_в_секунду = max(1, int(кадров_в_секунду))
        self._часы = часы
        self._lock = threading.Lock()
        self._есть = threading.Condition(self._lock)
        self._кадр = b""
        self.номер = 0
        self.когда = float("-inf")
        self._спрос = float("-inf")
        self._перезапуск = False
        self.беда = ""              # почему кадров нет, словами для человека
        self.браузер = ""           # какой в итоге открылся

    # --- хранилище: сюда кладёт браузер, отсюда берёт HTTP -----------------
    def положить(self, jpeg: bytes) -> None:
        with self._есть:
            self._кадр = jpeg
            self.номер += 1
            self.когда = self._часы()
            self._есть.notify_all()

    def кадр(self, после: int = 0, ждать: float = 0.0,
             не_старше: float | None = None) -> tuple[bytes, int]:
        """Кадр новее номера `после` — или (b"", после), если не дождались.

        `не_старше` — секунд: кадр старше этого не годится, ждём следующего.
        Так первый кадр потока не бывает застывшим кадром умершего браузера.
        """
        срок = self._часы() + ждать
        with self._есть:
            while True:
                годится = (self.номер > после and bool(self._кадр)
                           and (не_старше is None or self._часы() - self.когда <= не_старше))
                if годится:
                    return self._кадр, self.номер
                осталось = срок - self._часы()
                if осталось <= 0:
                    return b"", после
                self._есть.wait(min(осталось, 0.5))

    def сбросить(self) -> None:
        """Браузер остановлен — его последний кадр больше не кадр.

        Номер НЕ обнуляем: он растёт монотонно, и тот, кто ждёт «новее N»,
        должен дождаться именно нового, а не старого с номером 1. Без этого
        сброса после падения браузера робот получал прежний кадр мгновенно,
        поток обрывался на нём (беда), и лицо каждые пять секунд прыгало
        между «аватар с ПК» и «рисую сам» — всю минуту до новой попытки.
        """
        with self._есть:
            self._кадр = b""
            self.когда = float("-inf")
            self._есть.notify_all()

    def нужна(self, размер: tuple[int, int] | None = None) -> None:
        """Кто-то смотрит — снимать. Робот зовёт это на каждом кадре."""
        with self._lock:
            self._спрос = self._часы()
            if размер and tuple(размер) != tuple(self.размер):
                self.размер = (int(размер[0]), int(размер[1]))
                self._перезапуск = True

    def спрос_есть(self) -> bool:
        return self._часы() - self._спрос < self.ПРОСТОЙ

    # --- браузер --------------------------------------------------------------
    def запустить(self) -> None:
        threading.Thread(target=self._крутиться, name="съёмка", daemon=True).start()

    def _крутиться(self) -> None:
        while True:
            if not self.спрос_есть():
                time.sleep(0.25)
                continue
            try:
                self._снимать()
            except Exception as e:                  # noqa: BLE001
                self.беда = f"браузер не снялся: {e}"
                log.warning("съёмка аватара: %s — следующая попытка через %.0f с",
                            e, self.ПОВТОР)
                срок = self._часы() + self.ПОВТОР
                while self._часы() < срок:
                    time.sleep(0.5)

    def _открыть_браузер(self, p):
        """Системный Edge или Chrome, если есть; иначе — тот, что у Playwright.

        Свой Chromium Playwright скачивает отдельной командой
        (playwright install chromium) — полторы сотни мегабайт, которые на
        Windows чаще всего не нужны: Edge там уже стоит. Пробуем его первым.
        """
        # Без первого ключа Chrome прячет WebGL от программной отрисовки, а в
        # headless видеокарты может и не быть; без второго свежие сборки
        # вовсе отказывают WebGL без видеокарты — и Live2D молча не рисуется.
        ключи = ["--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]
        # Свой путь к браузеру — когда ни один из перечисленных не нашёлся
        # или нужен именно этот (например, Chromium из другой папки).
        свой = os.environ.get("KUZYA_BROWSER", "").strip()
        if свой:
            self.браузер = свой
            return p.chromium.launch(executable_path=свой, headless=True, args=ключи)
        последняя = None
        for канал in ("msedge", "chrome", None):
            try:
                браузер = p.chromium.launch(channel=канал, headless=True, args=ключи)
                self.браузер = канал or "chromium"
                return браузер
            except Exception as e:                  # noqa: BLE001
                последняя = str(e).splitlines()[0]
        raise RuntimeError(
            f"ни Edge, ни Chrome, ни Chromium Playwright не открылись ({последняя}). "
            f"Поставить свой: playwright install chromium — или указать путь к "
            f"любому Chrome/Edge/Chromium в переменной KUZYA_BROWSER")

    @staticmethod
    def _жалоба_страницы(страница) -> str:
        try:
            return str(страница.evaluate(
                "(document.getElementById('беда') || {}).textContent || ''")).strip()
        except Exception as e:                      # noqa: BLE001
            return f"страница не отвечает: {str(e).splitlines()[0]}"

    def _снимать(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.беда = "нет Playwright — pip install playwright"
            raise RuntimeError(self.беда) from None
        with sync_playwright() as p:
            браузер = self._открыть_браузер(p)
            try:
                while self.спрос_есть():
                    self._перезапуск = False
                    ширина, высота = self.размер
                    контекст = браузер.new_context(
                        viewport={"width": ширина, "height": высота},
                        device_scale_factor=1)
                    страница = контекст.new_page()
                    # «commit», а не «load»: страница тянет три скрипта с
                    # чужих CDN, и без интернета «load» не наступит никогда —
                    # а снимать надо всё, что она показывает, включая свою
                    # же надпись о том, чего ей не хватает.
                    # Ошибки страницы, которых нет в #беда (см. index.html):
                    # исключение прямо в отрисовке кадра ломает оверлей молча
                    # — модель дышит своей анимацией покоя, рот не открывается,
                    # сценки не играют, а на экране никакого текста об этом
                    # нет (тот текст льётся из показать(), а не из console.*).
                    # Здесь никто эту страницу не смотрит глазами — без этих
                    # хуков такая поломка была бы не видна вообще нигде.
                    страница.on("pageerror", lambda e: log.warning(
                        "страница аватара: необработанная ошибка JS: %s", e))
                    # И предупреждения тоже: страница ими говорит о том, что
                    # кадр рисуется, но не тем, чем задумано, — например, что
                    # у модели нет группы покоя и она будет стоять столбом.
                    # Раньше сюда шли только ошибки, и такие вещи оставались
                    # видны лишь в консоли невидимого браузера, то есть нигде.
                    страница.on("console", lambda m: log.warning(
                        "страница аватара: console.%s: %s", m.type, m.text)
                        if m.type in ("error", "warning") else None)
                    страница.goto(self.адрес, wait_until="commit", timeout=15000)
                    cdp = контекст.new_cdp_session(страница)

                    def принять_кадр(п: dict) -> None:
                        import base64
                        self.положить(base64.b64decode(п["data"]))
                        # Без подтверждения Chrome следующий кадр не пришлёт.
                        cdp.send("Page.screencastFrameAck",
                                 {"sessionId": п["sessionId"]})

                    cdp.on("Page.screencastFrame", принять_кадр)
                    # Страница рисует на каждый кадр монитора (60 Гц); роботу
                    # столько не надо — и не потянет декодировать.
                    cdp.send("Page.startScreencast", {
                        "format": "jpeg", "quality": self.КАЧЕСТВО,
                        "maxWidth": ширина, "maxHeight": высота,
                        "everyNthFrame": max(1, round(60 / self.кадров_в_секунду)),
                    })
                    self.беда = ""
                    log.info("съёмка аватара: %s, %d×%d, ~%d к/с",
                             self.браузер, ширина, высота, self.кадров_в_секунду)
                    жалоба_была = ""
                    следующий_осмотр = 0.0
                    while self.спрос_есть() and not self._перезапуск:
                        # Обработчики событий у синхронного Playwright
                        # срабатывают только пока мы внутри его вызова.
                        страница.wait_for_timeout(200)
                        if self._часы() - self.когда > self.ТИШИНА:
                            # Скринкаст шлёт кадр только на ИЗМЕНЕНИЕ
                            # картинки: страница, которая не двигается
                            # (надпись об ошибке, застывшая модель), не даёт
                            # ни одного — проверено. Тогда снимаем обычным
                            # скриншотом: раз в секунду это ничего не стоит.
                            self.положить(страница.screenshot(
                                type="jpeg", quality=self.КАЧЕСТВО))
                        if self._часы() < следующий_осмотр:
                            continue
                        следующий_осмотр = self._часы() + 5.0
                        # Страницу здесь никто не видит — её жалобы (нет
                        # модели, не тот путь в config.json) иначе не дошли
                        # бы ни до кого, а робот показывал бы их как есть.
                        жалоба = self._жалоба_страницы(страница)
                        if жалоба != жалоба_была:
                            жалоба_была = жалоба
                            if жалоба:
                                log.warning("страница аватара: %s", жалоба)
                            else:
                                log.info("страница аватара: всё в порядке")
                    cdp.send("Page.stopScreencast")
                    контекст.close()
            finally:
                браузер.close()
                # Что бы ни остановило браузер — простой или падение, — его
                # последний кадр остаётся в памяти, а живым он больше не
                # станет. Робот, придя за потоком, обязан ждать нового.
                self.сбросить()
                log.info("съёмка аватара остановлена: робот не смотрит")


# Типы того, что реально лежит в pc/avatar/: страница, настройка и файлы
# Live2D-модели (.model3.json — по сути JSON, но расширение своё у Cubism).
АВАТАР_ТИПЫ = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".css": "text/css; charset=utf-8",
    ".wav": "audio/wav",                  # у моделей бывают звуки к motion
    ".mp3": "audio/mpeg",
    # .moc3 — двоичный формат Cubism без зарегистрированного типа; загрузчик
    # берёт его как ArrayBuffer, тип ему безразличен. Записан явно, чтобы
    # было видно, что это не забытый, а честный octet-stream.
    ".moc3": "application/octet-stream",
    # Старый формат Cubism 2 (samples/shizuku): модель и движения — тоже
    # двоичные, тот же честный octet-stream.
    ".moc": "application/octet-stream",
    ".mtn": "application/octet-stream",
}


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
        # «/avatar» без хвостового «/» — не то же самое, что «/avatar/»: все
        # относительные пути внутри страницы (config.json, state, сама
        # модель) считаются от последнего, а без него браузер отрезает
        # «avatar» и просит /config.json у корня сервера — там пусто, и
        # оттуда приходит настоящая JSON-ошибка, которую fetch не отличает
        # от настоящих настроек. Раз и навсегда — редиректом, как это делают
        # обычные веб-сервера для каталогов.
        if self.path.split("?", 1)[0] == "/avatar":
            self.send_response(301)
            self.send_header("Location", "/avatar/")
            # HTTP/1.1 держит соединение живым и без длины тела не знает, где
            # кончился ответ, — клиент виснет на чтении до тайм-аута вместо
            # того, чтобы сразу увидеть редирект. Тело и так пустое.
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
                "голос_чей": getattr(getattr(cfg, "voice", None), "speaker", ""),
                "узнаю_по_голосу": sorted(
                    getattr(getattr(cfg, "who", None), "people", {})),
                # Поедет ли аватар на экран робота. Робот по этому решает,
                # тянуть ли поток или сразу рисовать самому.
                "аватар_поток": getattr(self.server.avatar, "съёмка", None) is not None,
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
        if path == "/avatar" or path.startswith("/avatar/"):
            self._avatar_get(path[len("/avatar"):].lstrip("/"))
            return
        # Мост к Open-LLM-VTuber (pc/ollv_bridge.py) — их провайдеры-заглушки
        # спрашивают отсюда, что сейчас говорит робот. Моста нет (OLLV_URL не
        # задан) — маршрутов тоже нет, честные 404, а не пустой ответ.
        мост = getattr(self.server, "мост_ollv", None)
        if path == "/ollv/line":
            if мост is None:
                self._json(404, {"error": "мост к OLLV не поднят (OLLV_URL не задан)"})
            else:
                self._json(200, мост.строка())
            return
        if path == "/ollv/audio":
            if мост is None:
                self._json(404, {"error": "мост к OLLV не поднят (OLLV_URL не задан)"})
                return
            звук = мост.звук()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(звук)))
            self.end_headers()
            self.wfile.write(звук)
            return
        self._json(404, {"error": "нет такого адреса"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/v1/messages") or path.endswith("/messages"):
            self._messages()
        elif path.endswith("/stt"):
            self._stt()
        elif path.endswith("/see"):
            self._see()
        elif path.endswith("/tts"):
            self._tts()
        elif path.endswith("/voice/confirm"):
            self._confirm()
        elif path.endswith("/voice/forget"):
            self._forget()
        elif path.endswith("/avatar/state"):
            self._avatar_post()
        else:
            self._json(404, {"error": "нет такого адреса"})

    # --- аватар: страница, настройка модели, приём состояния от робота -----
    АВАТАР_ПАПКА = Path(__file__).resolve().parent / "avatar"

    def _avatar_get(self, хвост: str) -> None:
        # Путь в запросе закодирован браузером: «model/Кузя v2.moc3» приходит
        # как «model/%D0%9A...%20v2.moc3», и без раскодирования файла «нет»
        # (404), хотя он лежит на месте. Раскодируем ДО склейки с папкой;
        # проверка на побег из папки ниже смотрит уже на раскодированный путь
        # — иначе «%2e%2e/» прошёл бы мимо неё.
        from urllib.parse import unquote
        хвост = unquote(хвост)
        if хвост == "state":
            self._json(200, self.server.avatar.состояние())
            return
        if хвост == "stream":
            self._avatar_stream()
            return
        if хвост == "frame.jpg":
            self._avatar_frame()
            return
        файл = (self.АВАТАР_ПАПКА / (хвост or "index.html")).resolve()
        # Не выйти за пределы папки: «..» в пути мог бы отдать любой файл ПК.
        try:
            файл.relative_to(self.АВАТАР_ПАПКА.resolve())
        except ValueError:
            self._json(404, {"error": "нет такого файла"})
            return
        if not файл.is_file():
            self._json(404, {
                "error": f"нет {хвост or 'index.html'} — см. pc/avatar/README.md: "
                         "туда нужно положить страницу и модель самому, один раз"})
            return
        тело = файл.read_bytes()
        if хвост == "config.json":
            тело = self._настройки_с_местными(тело)
        self.send_response(200)
        self.send_header("Content-Type",
                         АВАТАР_ТИПЫ.get(файл.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(тело)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(тело)

    def _настройки_с_местными(self, тело: bytes) -> bytes:
        """config.json плюс config.local.json поверх — своё отдельно от общего.

        Путь к модели у каждого свой, а config.json лежит в репозитории. На
        живом ПК из-за этого не проходил git pull: «Your local changes to
        pc/avatar/config.json would be overwritten» — и ПК три часа крутил
        старый код, а на экране робота не появлялся персонаж. Своё теперь в
        config.local.json (его git не видит), и pull больше ни с чем не спорит.
        """
        местный = self.АВАТАР_ПАПКА / "config.local.json"
        if not местный.is_file():
            return тело
        try:
            общее = json.loads(тело.decode("utf-8"))
            своё = json.loads(местный.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            log.warning("аватар: config.local.json не разобрался (%s) — беру общий", e)
            return тело
        for ключ, значение in своё.items():
            # Словари («параметры», «эмоции») дополняем, остальное заменяем.
            if isinstance(значение, dict) and isinstance(общее.get(ключ), dict):
                общее[ключ] = {**общее[ключ], **значение}
            else:
                общее[ключ] = значение
        # СТАРЫЙ ПУТЬ К МОДЕЛИ СИЛЬНЕЕ НОВОГО — и это ловушка. В репозитории
        # теперь есть готовые модели (avatar/samples/), и config.json на них
        # и показывает; но у того, кто раньше вписал свою модель в
        # config.local.json, git его не трогает — а если той модели на диске
        # уже нет (переставил, переименовал, не докачал), страница покажет
        # «не загрузил модель» и всё. Молча остаться без персонажа хуже, чем
        # показать не того: берём общий путь и говорим об этом в лог.
        свой_путь = своё.get("модель")
        if свой_путь and not self._модель_на_месте(свой_путь):
            общий_путь = json.loads(тело.decode("utf-8")).get("модель", "")
            # А если и общей нет (её тоже могли снести) — оставляем ПУТЬ
            # ПОЛЬЗОВАТЕЛЯ. Страница тогда скажет «не загрузил модель
            # «<его путь>»», и это подсказка; подмена на другой битый путь
            # только запутала бы: человек ищет опечатку там, где её нет.
            if self._модель_на_месте(общий_путь):
                log.warning("аватар: модели «%s» из config.local.json нет на диске — "
                            "беру общую «%s». Поправь путь или убери строку.",
                            свой_путь, общий_путь)
                общее["модель"] = общий_путь
            else:
                log.warning("аватар: модели «%s» из config.local.json нет на диске, "
                            "и общая «%s» тоже не на месте — экран останется без "
                            "персонажа. Проверь пути и что папка samples/ на месте.",
                            свой_путь, общий_путь)
        return json.dumps(общее, ensure_ascii=False).encode("utf-8")

    def _модель_на_месте(self, путь) -> bool:
        """Есть ли такой файл модели ВНУТРИ avatar/ — как его увидит страница.

        Не просто is_file(): страница просит модель у этого же сервера, а тот
        наружу из avatar/ не отдаёт (см. _avatar_get). Путь «../чужое.json»
        или «/etc/hostname» на диске существовать может, а по HTTP всё равно
        обернётся 404 — то есть «файл есть» тут значило бы не то, что нужно.
        """
        if not isinstance(путь, str) or not путь:
            return False
        цель = (self.АВАТАР_ПАПКА / путь).resolve()
        try:
            цель.relative_to(self.АВАТАР_ПАПКА.resolve())
        except ValueError:
            return False
        return цель.is_file()

    def _avatar_post(self) -> None:
        try:
            данные = json.loads(self._body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"error": f"тело не JSON: {e}"})
            return
        self.server.avatar.принять(данные)
        self._json(200, {"ok": True})

    # Сколько ждать первого кадра. Браузер поднимается по первому же спросу,
    # и холодный старт Edge с загрузкой модели занимает секунды; робот тем
    # временем рисует лицо сам и ничего не теряет.
    ПЕРВЫЙ_КАДР = 20.0
    # Граница частей MJPEG. Любая строка, лишь бы не встречалась в JPEG.
    ГРАНИЦА = b"kuzya-frame"
    # Как часто повторять прежний кадр, когда новых нет. Робот считает поток
    # мёртвым через полторы секунды тишины — пульс должен быть заметно чаще.
    ПУЛЬС = 0.5
    # Сколько ждать, пока робот заберёт написанное. Без срока пропавший робот
    # (выключили питание, отвалилась сеть) держал этот обработчик — и
    # headless-браузер вместе с ним — до тайм-аута TCP, четверть часа.
    # То же делает web/server.py для своих SSE.
    ПОТОК_ТАЙМАУТ = 5.0

    def _размер_запрошен(self) -> tuple[int, int] | None:
        """?w=1280&h=800 — экран робота; страница рисуется ровно под него."""
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        try:
            w, h = int(q["w"][0]), int(q["h"][0])
        except (KeyError, ValueError, IndexError):
            return None
        return (w, h) if 64 <= w <= 4096 and 64 <= h <= 4096 else None

    def _съёмка(self):
        """Съёмка, если она есть, иначе — 503 с причиной и None."""
        съёмка = getattr(self.server.avatar, "съёмка", None)
        if съёмка is None:
            self._json(503, {"error": "съёмка аватара не поднята: нужен Playwright "
                                      "(pip install playwright) или снят ключ "
                                      "--no-avatar-stream"})
            return None
        съёмка.нужна(self._размер_запрошен())
        if съёмка.беда:
            # Браузер упал, следующая попытка через ПОВТОР. Робот узнаёт об
            # этом сразу и рисует сам, а не получает застывший кадр с
            # прошлой жизни браузера и не прыгает между двумя лицами.
            self._json(503, {"error": съёмка.беда})
            return None
        return съёмка

    def _avatar_frame(self) -> None:
        съёмка = self._съёмка()
        if съёмка is None:
            return
        кадр, _ = съёмка.кадр(0, ждать=self.ПЕРВЫЙ_КАДР, не_старше=съёмка.СВЕЖИЙ)
        if not кадр:
            self._json(503, {"error": съёмка.беда or "кадров ещё нет"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(кадр)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(кадр)

    def _avatar_stream(self) -> None:
        """MJPEG: одна HTTP-ответ, кадры друг за другом, пока робот слушает.

        Старейший из потоковых форматов и единственный, который робот
        разберёт без видеокарты и без кодеков: каждая часть — обычный JPEG,
        pygame читает его сам. Сжатия между кадрами нет, зато нет и задержки
        на буферизацию, а по домашней сети мегабайта в секунду хватает.
        """
        съёмка = self._съёмка()
        if съёмка is None:
            return
        кадр, номер = съёмка.кадр(0, ждать=self.ПЕРВЫЙ_КАДР, не_старше=съёмка.СВЕЖИЙ)
        if not кадр:
            self._json(503, {"error": съёмка.беда or "кадров ещё нет"})
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={self.ГРАНИЦА.decode()}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        log.info("робот подключился к потоку аватара")
        try:
            self.connection.settimeout(self.ПОТОК_ТАЙМАУТ)
        except OSError:
            pass
        try:
            while True:
                self.wfile.write(b"--" + self.ГРАНИЦА + b"\r\n"
                                 b"Content-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(кадр)).encode() + b"\r\n\r\n"
                                 + кадр + b"\r\n")
                self.wfile.flush()
                съёмка.нужна()
                новый_кадр, новый = съёмка.кадр(номер, ждать=self.ПУЛЬС)
                if новый_кадр:
                    кадр, номер = новый_кадр, новый
                elif съёмка.беда or self.server.avatar.съёмка is not съёмка:
                    # Браузер упал. Роботу честнее увидеть обрыв и рисовать
                    # самому, чем смотреть на застывший последний кадр.
                    break
                # Иначе — страница просто не меняется (Chrome шлёт кадр
                # только на изменение). Повторяем прежний: робот судит о
                # живости ПК по потоку, а не по тому, моргает ли модель.
        except (ConnectionError, BrokenPipeError, OSError):
            pass
        log.info("робот отключился от потока аватара")

    # --- кто говорит ---
    def _who(self):
        return getattr(self.server.cfg, "who", None)

    def _name_asked(self) -> str:
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(self.path).query).get("имя", [""])[0]
                or parse_qs(urlparse(self.path).query).get("name", [""])[0]).strip()

    def _confirm(self) -> None:
        """«Это говорили мне» — только теперь голос попадает в память.

        Раздельно с /stt намеренно: узнавать надо каждую фразу, а запоминать
        только обращённые к роботу. Иначе телевизор, который говорит в комнате
        больше всех, растащил бы слепки по дикторам.
        """
        who = self._who()
        if who is None:
            self._json(503, {"error": "узнавание по голосу не поднято"})
            return
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)
        метка = (query.get("tag", [""])[0] or "").strip()
        if not метка:
            self._json(400, {"error": "нужна метка фразы"})
            return
        try:
            кто = who.confirm(метка, self._name_asked())
        except Exception as e:
            log.exception("не смог запомнить голос")
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"кто": кто,
                         "фраз": who.people.get(кто, {}).get("фраз", 0)})

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
        # Тот же звук — аватару, под рот. 44 байта — заголовок wav из _wav().
        аватар = getattr(self.server, "avatar", None)
        if аватар is not None:
            try:
                аватар.озвучил(wav[44:], voice.RATE)
            except Exception:                       # noqa: BLE001
                log.exception("аватар: не разобрал звук под рот")
        # И туда же, если поднят мост к Open-LLM-VTuber: тот же текст, та
        # же эмоция, тот же звук — без второго синтеза и лишней нагрузки.
        мост = getattr(self.server, "мост_ollv", None)
        if мост is not None:
            try:
                эмоция = аватар.состояние().get("эмоция", "спокоен") if аватар else "спокоен"
                мост.готово(text, эмоция, wav[44:], voice.RATE)
            except Exception:                       # noqa: BLE001
                log.exception("мост к OLLV: не передал фразу")
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        # Каким голосом это прозвучало. Робот называет этим именем свой кэш:
        # иначе после смены --voice заготовки продолжают звучать прежним
        # голосом, а новые фразы — новым, и голос «прорезается» через раз.
        #
        # Через getattr намеренно: синтез уже удался, звук лежит готовый, и
        # ронять запрос из-за подсказки нельзя. Без заголовка робот всего лишь
        # спросит голос отдельно, через /health.
        кто = getattr(voice, "кто", None)
        if callable(кто):
            self.send_header("X-Voice", кто(req.get("voice") or ""))
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
        кто, похожесть, метка = "", 0.0, ""
        who = self._who()
        if who is not None and text:
            кто, похожесть, метка = who.identify(wav, _wav_seconds(wav))
        self._json(200, {"text": text, "sure": sure, "кто": кто,
                         "похожесть": round(похожесть, 3), "метка": метка})

    def _see(self) -> None:
        """Разглядеть кадр с камеры робота.

        Отдельная ручка, а не картинка внутри обычного разговора, — и это не
        прихоть. to_ollama_messages разбирает только текст, вызовы
        инструментов и их итоги: блок с изображением он роняет молча, без
        единой ошибки. Модель, не увидев ничего, честно и уверенно расскажет,
        что перед роботом, — а робот это перескажет человеку. Такую поломку
        не видно ни в логах, ни на слух, поэтому зрение ходит своей дорогой,
        где картинка либо доходит, либо отказ говорится вслух.
        """
        try:
            req = json.loads(self._body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"error": f"тело не JSON: {e}"})
            return
        кадр = (req.get("кадр") or "").strip()
        if not кадр:
            self._json(400, {"error": "кадра нет"})
            return
        вопрос = (req.get("вопрос") or "Что на картинке?").strip()
        модель = VISION_MODEL
        payload = {
            "model": модель,
            "messages": [{"role": "user", "content": вопрос, "images": [кадр]}],
            "stream": False,
            "options": {"num_predict": 300},
        }
        начали = time.monotonic()
        try:
            with self.server.cfg.ollama._post("/api/chat", payload,
                                              stream=False) as ответ:
                данные = json.loads(ответ.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            тело = e.read().decode("utf-8", "replace")[:300]
            # Самая частая беда — зрячей модели просто нет. Сказать об этом
            # прямо дешевле, чем оставить человека гадать: починка — одна
            # команда, и она должна быть написана в журнале.
            log.warning("зрение: Ollama отказала (%s) %s", e.code, тело)
            подсказка = (f" Поставь зрячую модель: ollama pull {модель}"
                         if "not found" in тело.lower() else "")
            self._json(502, {"error": f"модель {модель} не ответила.{подсказка}"})
            return
        except Exception as e:                  # noqa: BLE001
            log.warning("зрение: не вышло (%s)", e)
            self._json(502, {"error": str(e)})
            return
        текст = ((данные.get("message") or {}).get("content") or "").strip()
        log.info("зрение: %s разглядела кадр за %.1f с", модель,
                 time.monotonic() - начали)
        self._json(200, {"текст": текст, "модель": модель})

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

        # Пока идёт разговор, режиссёр сценок (Аватар._позвать_режиссёра) к
        # модели не лезет: очередь Ollama одна, и его вопрос задержал бы
        # ответ человеку.
        аватар = getattr(self.server, "avatar", None)
        if аватар is not None:
            аватар.разговор_начался()
        try:
            if req.get("stream"):
                self._stream(model, messages, tools, limit)
            else:
                self._whole(model, messages, tools, limit)
        finally:
            if аватар is not None:
                аватар.разговор_кончился()

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
        сказано: list[str] = []
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
                    сказано.append(clean)
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
                    сказано.append(tail)
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
            сказано.append(tail)
            yield ("text", tail, None)
        # Весь ответ целиком — аватару: настроение персонажа берётся из
        # того, что модель сказала, потому что больше его брать неоткуда.
        аватар = getattr(self.server, "avatar", None)
        if аватар is not None:
            try:
                аватар.ответил("".join(сказано))
            except Exception:                       # noqa: BLE001
                log.exception("аватар: не разобрал ответ")
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
    p.add_argument("--stt", default="whisper", choices=("whisper", "gigaam"),
                   help="чем распознавать речь: whisper (по умолчанию) или "
                        "gigaam — русское распознавание Сбера, точнее на "
                        "коротких словах, но без уверенности")
    p.add_argument("--gigaam-model", default="v3_e2e_rnnt",
                   help="какая модель GigaAM, если выбран --stt gigaam")
    p.add_argument("--wake", default="Кузя",
                   help="как зовут робота — подсказка распознаванию, "
                        "чтобы имя не превращалось в «Уйди» и «Кудяка»")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--host", default="0.0.0.0",
                   help="0.0.0.0 — слышно роботу по сети, 127.0.0.1 — только этой машине")
    p.add_argument("--ollama", default=OLLAMA)
    p.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                   help=f"окно контекста Ollama в токенах (по умолчанию "
                        f"{DEFAULT_CTX}). Умолчание самой Ollama — 4096, а "
                        "постоянная часть запроса робота это примерно четыре "
                        "тысячи токенов: в 4096 он не помещается никогда, и "
                        "Ollama молча срезает начало вместе с промптом. "
                        "Меньше ставить нельзя, больше — если хватает "
                        "видеопамяти под KV-кэш")
    p.add_argument("--think", action="store_true",
                   help="разрешить модели размышлять вслух: точнее с "
                        "инструментами, но ответ идёт в разы дольше")
    p.add_argument("--voice", default=Voice.VOICES[0],
                   help="голос синтеза: " + "|".join(Voice.VOICES) +
                        " либо «нет», чтобы робот говорил своим piper. "
                        "eugene — мужской ровный, aidar — мужской мягче и "
                        "теплее, baya — женский спокойный, kseniya и xenia — "
                        "женские, xenia самый живой")
    p.add_argument("--no-voiceprints", action="store_true",
                   help="не узнавать людей по голосу")
    p.add_argument("--no-avatar-stream", action="store_true",
                   help="не снимать аватар для экрана робота (робот тогда "
                        "рисует лицо сам, как раньше)")
    p.add_argument("--avatar-fps", type=int, default=10,
                   help="сколько кадров аватара в секунду слать роботу; "
                        "больше — плавнее, но робот декодирует каждый на "
                        "процессоре")
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

    ollama = Ollama(args.ollama, think=args.think, ctx=args.ctx)
    рядом = Path(os.environ.get("HF_HOME", Path.home() / ".cache")) / "кузя"
    слух = Whisper(args.whisper, wake=args.wake)
    if args.stt == "gigaam":
        # Проверяем, что он вообще поднимется, прямо здесь: узнать об этом при
        # запуске честнее, чем на первой же фразе, когда человек уже говорит.
        # Сам прогрев дальше сделает общий поток — иначе он идёт дважды.
        кандидат = GigaAM(args.gigaam_model)
        try:
            import gigaam                      # noqa: F401
            if not кандидат.ffmpeg_есть():
                raise RuntimeError(
                    "нет ffmpeg — GigaAM читает звук только через него. "
                    "Windows: winget install Gyan.FFmpeg; "
                    "Debian: apt install ffmpeg")
            слух = кандидат
        except Exception as e:                  # noqa: BLE001
            log.warning("GigaAM не поднялся (%s) — распознаю Whisper'ом", e)
    cfg = Config(args.model, слух, ollama,
                 None if args.voice == "нет" else Voice(speaker=args.voice),
                 None if args.no_voiceprints else Voiceprints(рядом / "голоса.json"))

    if not ollama.alive():
        log.warning("Ollama по адресу %s не отвечает. Запустите её и оставьте "
                    "висеть в трее — без неё разговаривать не с чем.", args.ollama)
    else:
        have = ollama.models()
        # Рассуждающую модель меняем на нерассуждающую, если такая скачана.
        # Это самая крупная одиночная правка скорости из всех: размышления
        # наружу не идут, а время на них тратится целиком, и всё это время
        # робот молчит.
        выбрана, что_сказать = выбрать_модель(args.model, have)
        if что_сказать:
            log.warning("%s", что_сказать)
        if выбрана != args.model:
            args.model = cfg.model = выбрана
        if not any(m == args.model or m.startswith(args.model + ":") for m in have):
            log.warning("модель %s не скачана. Скачать: ollama pull %s",
                        args.model, args.model)
            log.warning("сейчас есть: %s", ", ".join(have) or "ничего")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.cfg = cfg
    srv.avatar = Аватар()
    # Свои праздники и дни рождения — из config.local.json («праздники»:
    # {"05-12": "С днём рождения, Игорь!"}), к встроенным датам сценария.
    srv.avatar.сценарист = scenes.Сценарист(праздники=свои_праздники(Handler.АВАТАР_ПАПКА))
    # Мост к Open-LLM-VTuber — по умолчанию его нет, свой аватар работает
    # как раньше. Задан OLLV_URL — поднимаем: их провайдеры-заглушки
    # (pc/ollv_bridge/README.md) спрашивают фразу здесь же, этим сервером.
    ollv_url = os.environ.get("OLLV_URL", "").strip()
    if ollv_url:
        srv.мост_ollv = ollv_bridge.Мост(ollv_url)
        srv.мост_ollv.start()
        log.info("мост к Open-LLM-VTuber: %s", ollv_url)

    def спросить_настроение(текст: str) -> str:
        """Одним коротким запросом к той же модели: пять токенов, доли секунды."""
        ответ = ""
        for часть in ollama.chat(cfg.model, [{"role": "user",
                                              "content": ВОПРОС_О_НАСТРОЕНИИ + текст[:400]}],
                                 [], 8):
            ответ += (часть.get("message") or {}).get("content") or ""
        return ответ

    srv.avatar.спросить_настроение = спросить_настроение

    def спросить_режиссёра(вопрос: str) -> str:
        """План сценок — той же моделью, коротко: имена из списка и реплика."""
        ответ = ""
        for часть in ollama.chat(cfg.model, [{"role": "user", "content": вопрос}],
                                 [], 160):
            ответ += (часть.get("message") or {}).get("content") or ""
        return ответ

    srv.avatar.спросить_режиссёра = спросить_режиссёра
    if not args.no_avatar_stream:
        задана, путь_модели = модель_настроена()
        if not задана:
            # Модель никто не вписал — и это не повод открывать браузер.
            # Раньше здесь съёмка стартовала всегда, а config.json по
            # умолчанию показывал на образец Shizuku (аниме-девушка из
            # samples/): любой, кто просто запустил kuzya_pc.py, получал на
            # экране робота не Кузю, а её. Теперь без своей модели браузер
            # не трогаем вовсе — робот рисует домовёнка сам, как исходно.
            log.info("аватар на ПК не настроен — робот рисует Кузю сам "
                     "(pc/avatar/README.md, если нужен Live2D или 3D-персонаж: "
                     "путь к модели вписывается в pc/avatar/config.local.json)")
        else:
            try:
                import playwright  # noqa: F401
            except ImportError:
                log.warning("модель есть (%s), но аватар на экран робота не поедет: "
                            "нет Playwright. Поставить: pip install playwright — и "
                            "Edge или Chrome на этом ПК. Робот пока рисует лицо сам.",
                            путь_модели)
            else:
                # Какую страницу снимать — Live2D или 3D — решает
                # config.local.json («движок»: «live2d» или «3d»). Обе живут
                # рядом и обе рабочие: 3D даёт скелет, к которому подходят
                # чужие анимации (Mixamo, .vrma) и физику волос-одежды,
                # Live2D — нарисованные автором кадры. Что лучше — видно
                # только глазами, поэтому выбор оставлен человеку, а не зашит.
                съёмка = Съёмка(f"http://127.0.0.1:{args.port}/avatar/{страница_аватара()}",
                                кадров_в_секунду=args.avatar_fps)
                съёмка.запустить()
                srv.avatar.съёмка = съёмка
    srv.daemon_threads = True
    log.info("мозг на %s:%d | модель %s | распознавание %s | голос %s | "
             "сборка %s", args.host, args.port, args.model,
             # Имя берём у того, кто и правда будет слушать: раньше здесь
             # стоял аргумент командной строки, и шапка бодро сообщала про
             # Whisper, когда работал GigaAM.
             cfg.whisper.size.rsplit("/", 1)[-1],
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
