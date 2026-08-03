#!/usr/bin/env python3
"""Самопроверка моста «Anthropic → Ollama» — без Ollama, без видеокарты.

Смысл проверки. Мост притворяется сервером Anthropic, и разговаривает с ним
не человек, а клиентская библиотека — придирчивая: события в потоке должны
идти строго в своём порядке, иначе она бросит исключение посреди фразы, и
робот замолчит на полуслове. Проверить это глазами невозможно, поэтому здесь
поднимается настоящий сервер, а вместо Ollama подставляется заглушка, и
запрос делает НАСТОЯЩИЙ клиент anthropic — тот же самый, что стоит на роботе.

    python pc/selftest.py
"""

from __future__ import annotations

import sys
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kuzya_pc                                          # noqa: E402
from kuzya_pc import (Config, Handler, Whisper,          # noqa: E402
                      to_ollama_messages, to_ollama_tools)

FAILED: list[str] = []


def check(what: str, got, expected) -> None:
    if got != expected:
        FAILED.append(f"{what}\n      получено: {got!r}\n      ожидалось: {expected!r}")


def section(title: str) -> None:
    print(f"\n== {title}")


# --------------------------------------------------------------------------
def test_messages() -> None:
    section("перевод переписки")

    out = to_ollama_messages("ты робот", [{"role": "user", "content": "привет"}])
    check("системный промпт стал первым сообщением", out[0],
          {"role": "system", "content": "ты робот"})
    check("реплика человека", out[1], {"role": "user", "content": "привет"})

    # Системный промпт приезжает и списком блоков — так его шлют, когда
    # просят кэширование.
    out = to_ollama_messages([{"type": "text", "text": "ты робот"}], [])
    check("промпт списком блоков", out[0]["content"], "ты робот")

    # Ход с инструментом: у Anthropic результат лежит внутри реплики человека,
    # у Ollama это отдельное сообщение с ролью tool. Если сложить как есть,
    # модель решит, что человек зачитал ей вслух служебный вывод.
    out = to_ollama_messages(None, [
        {"role": "user", "content": "который час"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "сейчас гляну"},
            {"type": "tool_use", "id": "t1", "name": "time_now", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "20:15"},
        ]},
    ])
    check("вызов инструмента у ассистента",
          out[1].get("tool_calls"), [{"function": {"name": "time_now", "arguments": {}}}])
    check("текст ассистента сохранён", out[1]["content"], "сейчас гляну")
    check("результат стал сообщением tool", out[2],
          {"role": "tool", "tool_name": "time_now", "content": "20:15"})

    # Результат инструмента бывает и списком блоков.
    out = to_ollama_messages(None, [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "battery", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t9",
             "content": [{"type": "text", "text": "12.4 вольта"}]}]},
    ])
    check("результат списком блоков", out[1]["content"], "12.4 вольта")

    schemas = to_ollama_tools([
        {"name": "drive", "description": "ехать",
         "input_schema": {"type": "object", "properties": {"d": {"type": "string"}}}},
    ])
    check("схема обёрнута в function", schemas[0]["function"]["name"], "drive")
    check("параметры на месте",
          schemas[0]["function"]["parameters"]["properties"]["d"]["type"], "string")


# --------------------------------------------------------------------------
class FakeOllama:
    """Вместо настоящей Ollama. Отдаёт заранее заданный ответ по кускам."""

    def __init__(self, chunks: list[str], calls: list[tuple] = ()) -> None:
        self.chunks = chunks
        self.calls = list(calls)
        self.seen: dict | None = None

    def chat(self, model, messages, tools, max_tokens):
        self.seen = {"model": model, "messages": messages, "tools": tools,
                     "max_tokens": max_tokens}
        for c in self.chunks:
            yield {"message": {"role": "assistant", "content": c}, "done": False}
        for name, args in self.calls:
            yield {"message": {"role": "assistant", "content": "",
                               "tool_calls": [{"function": {"name": name,
                                                            "arguments": args}}]},
                   "done": False}
        yield {"done": True, "done_reason": "stop",
               "prompt_eval_count": 123, "eval_count": 45}

    def alive(self):
        return True

    def models(self):
        return ["тест"]


class Broken(FakeOllama):
    """Ollama ответила отказом: не запущена, модель не скачана, опечатка."""

    def chat(self, *a, **kw):
        raise ConnectionRefusedError("Ollama не запущена")
        yield  # pragma: no cover — делает функцию генератором


def serve(ollama) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # Whisper настоящий, но модель он грузит лениво — видеокарта не нужна.
    srv.cfg = Config("тест", whisper=Whisper("tiny"), ollama=ollama)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_stream() -> None:
    """Главная проверка: настоящий клиент anthropic против нашего моста."""
    section("настоящий клиент против моста")
    import anthropic

    fake = FakeOllama(["Привет", ", Игорь"])
    srv, url = serve(fake)
    try:
        client = anthropic.Anthropic(api_key="local", base_url=url, max_retries=0)
        pieces: list[str] = []
        with client.messages.stream(
            model="неважно", max_tokens=64,
            system="ты робот",
            tools=[{"name": "battery", "description": "заряд",
                    "input_schema": {"type": "object", "properties": {}}}],
            messages=[{"role": "user", "content": "привет"}],
        ) as stream:
            for event in stream:
                if (event.type == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"):
                    pieces.append(event.delta.text)
            final = stream.get_final_message()

        check("текст дошёл кусками", "".join(pieces), "Привет, Игорь")
        check("собранное сообщение", final.content[0].text, "Привет, Игорь")
        check("причина остановки", final.stop_reason, "end_turn")
        check("токены посчитаны",
              (final.usage.input_tokens, final.usage.output_tokens), (123, 45))
        check("схема инструмента доехала до Ollama",
              fake.seen["tools"][0]["function"]["name"], "battery")
        check("промпт стал системным сообщением",
              fake.seen["messages"][0]["role"], "system")
    finally:
        srv.shutdown()


def test_ping() -> None:
    """Пока модель думает, поток должен подавать знаки жизни.

    Размышления наружу не выходят, и на всё это время мост замолкает. На живом
    роботе это вышло в двадцать пять секунд тишины — ровно его таймаут чтения:
    он решил, что бесплатный ПК умер, и ушёл в платное облако прямо посреди
    ответа. Три с половиной тысячи оплаченных токенов за «сколько времени».

    Заодно проверяем, что клиент anthropic такие пинги принимает молча: они
    часть его же протокола, но приходят там, где он ждёт текст.
    """
    section("знаки жизни, пока модель думает")
    import anthropic

    было, kuzya_pc.PING_SECONDS = kuzya_pc.PING_SECONDS, -1.0
    try:
        # Модель думает вслух, потом отвечает. Всё до </think> придерживается,
        # то есть наружу в это время не идёт ничего, кроме пингов.
        fake = FakeOllama(["Надо подумать. " * 20, "Ещё подумать.",
                           "</think>", "Привет!"])
        srv, url = serve(fake)
        try:
            client = anthropic.Anthropic(api_key="local", base_url=url, max_retries=0)
            pieces: list[str] = []
            events: list[str] = []
            with client.messages.stream(
                model="неважно", max_tokens=64,
                messages=[{"role": "user", "content": "привет"}],
            ) as stream:
                for event in stream:
                    events.append(event.type)
                    if (event.type == "content_block_delta"
                            and getattr(event.delta, "type", "") == "text_delta"):
                        pieces.append(event.delta.text)
                stream.get_final_message()
            check("вслух только ответ", "".join(pieces), "Привет!")
            # Клиент пинги проглатывает молча — до событий они не доходят, и
            # проверять их наличие надо в сыром потоке, ниже.
            check("клиент на пингах не спотыкается", "message_stop" in events, True)

            import json
            import urllib.request
            req = urllib.request.Request(
                url + "/v1/messages",
                data=json.dumps({"model": "неважно", "max_tokens": 64,
                                 "stream": True,
                                 "messages": [{"role": "user", "content": "привет"}]}
                                ).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                сырое = resp.read().decode("utf-8")
            check("знаки жизни ушли в поток", "event: ping" in сырое, True)
            # И — главное — размышления в сыром потоке тоже отсутствуют.
            check("размышления не ушли даже сырыми", "Надо подумать" in сырое, False)
        finally:
            srv.shutdown()
    finally:
        kuzya_pc.PING_SECONDS = было


def test_whisper_fallback() -> None:
    """Чужой склад с моделью может переехать — робот от этого не глохнет.

    По умолчанию распознавание берётся русским дообучением с HuggingFace: оно
    заметно точнее стандартного, но живёт на чужом сайте. Если склад
    недоступен, обязана подняться обычная модель из стандартного набора.
    """
    section("запасная модель распознавания")
    w = Whisper("такой-модели-нет/вообще")
    tried: list[str] = []

    def fake(size):
        tried.append(size)
        if size != kuzya_pc.FALLBACK_WHISPER:
            raise OSError("склад не отвечает")
        return "модель"

    w._try = fake
    check("поднялась запасная", w._load(), "модель")
    check("сначала пробовали заказанную", tried,
          ["такой-модели-нет/вообще", kuzya_pc.FALLBACK_WHISPER])
    check("имя обновлено — /health не соврёт", w.size, kuzya_pc.FALLBACK_WHISPER)

    # А если и запасная не поднялась — врать нельзя, пусть падает.
    w = Whisper(kuzya_pc.FALLBACK_WHISPER)
    w._try = lambda size: (_ for _ in ()).throw(OSError("нет и её"))
    try:
        w._load()
        check("падение запасной", "промолчал", "исключение")
    except OSError:
        pass


def test_tts() -> None:
    """Голос с ПК: wav наружу, а отсутствие голоса — честная ошибка.

    Робот на ошибку отвечает переходом на свой piper, поэтому врать здесь
    нельзя: молчаливый успех обернулся бы немым роботом.
    """
    section("голос на ПК")
    import json
    import urllib.error
    import urllib.request

    from kuzya_pc import _wav

    # Заголовок wav собираем сами, без внешних библиотек, — проверим, что его
    # понимает стандартный разбор. Робот читает ответ именно им.
    import io
    import wave
    with wave.open(io.BytesIO(_wav(b"\x00\x01" * 100, 24000))) as w:
        check("частота в заголовке", w.getframerate(), 24000)
        check("моно", w.getnchannels(), 1)
        check("два байта на отсчёт", w.getsampwidth(), 2)
        check("длина звука", w.getnframes(), 100)

    class FakeVoice:
        ready = True

        def say(self, text, speaker=""):
            self.said = (text, speaker)
            return _wav(b"\x00\x01" * 240, 24000)

    fake = FakeOllama([])
    srv, url = serve(fake)
    voice = FakeVoice()
    srv.cfg.voice = voice
    try:
        def ask(payload):
            req = urllib.request.Request(
                url + "/tts", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=10)

        with ask({"text": "Привет!", "voice": "baya"}) as resp:
            body = resp.read()
            check("отдали звук", resp.headers.get("Content-Type"), "audio/wav")
        check("это настоящий wav", body[:4] + body[8:12], b"RIFFWAVE")
        check("фраза и голос доехали", voice.said, ("Привет!", "baya"))

        for пустое in ({"text": "   "}, {}):
            try:
                ask(пустое)
                check("пустую фразу не синтезируем", "промолчал", "ошибка 400")
            except urllib.error.HTTPError as e:
                check("пустую фразу не синтезируем", e.code, 400)

        # Голоса нет вовсе — робот должен узнать об этом, а не ждать молча.
        srv.cfg.voice = None
        try:
            ask({"text": "Привет!"})
            check("без голоса честная ошибка", "промолчал", "ошибка 503")
        except urllib.error.HTTPError as e:
            check("без голоса честная ошибка", e.code, 503)
    finally:
        srv.shutdown()


def test_warming() -> None:
    """Пока модель едет в видеопамять, мост отвечает сам — и бесплатно.

    Загрузка четырёхмиллиардной модели заняла на живом ПК семьдесят шесть
    секунд. Робот в это время сказал «Кузя, привет», не дождался ответа за
    свои двадцать пять секунд, счёл ПК мёртвым и ушёл в облако: одно «привет»
    обошлось в 9845 оплаченных токенов. Ответить самому — мгновенно, даром и
    честнее молчания.
    """
    section("пока мозг просыпается, отвечаем сами")
    import anthropic

    fake = FakeOllama(["не должно прозвучать"])
    fake.ready = False
    fake.started = kuzya_pc.time.monotonic()
    srv, url = serve(fake)
    try:
        client = anthropic.Anthropic(api_key="local", base_url=url, max_retries=0)
        pieces: list[str] = []
        with client.messages.stream(
            model="неважно", max_tokens=64,
            messages=[{"role": "user", "content": "привет"}],
        ) as stream:
            for event in stream:
                if (event.type == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"):
                    pieces.append(event.delta.text)
            final = stream.get_final_message()
        check("робот услышал честный ответ", "".join(pieces), kuzya_pc.WARMING_REPLY)
        check("ответ завершён по правилам", final.stop_reason, "end_turn")
        check("Ollama не тронута", fake.seen, None)

        # Прогрелись — дальше как обычно.
        fake.ready = True
        msg = client.messages.create(
            model="неважно", max_tokens=64,
            messages=[{"role": "user", "content": "привет"}])
        check("после прогрева отвечает модель", msg.content[0].text,
              "не должно прозвучать")

        # А если прогрев затянулся сверх всякой меры — Ollama, похоже, не
        # поднялась вовсе. Вечно отвечать «просыпаюсь» нельзя: робот должен
        # узнать правду и уйти в облако.
        fake.ready = False
        fake.started = kuzya_pc.time.monotonic() - kuzya_pc.WARMING_GRACE - 1
        msg = client.messages.create(
            model="неважно", max_tokens=64,
            messages=[{"role": "user", "content": "привет"}])
        check("вечно просыпаться не даём", msg.content[0].text, "не должно прозвучать")
    finally:
        srv.shutdown()


def test_think_switch() -> None:
    """think=false у Ollama значит «не разбирай размышления», а не «не думай».

    Выяснено прямым запросом к Ollama. С --think=false qwen3:4b думал 448
    токенов и вывалил рассуждения в content вместе с закрывающим тегом; без
    флага — думал столько же, но Ollama отдала их отдельно, и content пришёл
    чистым. То есть выключатель делает ХУЖЕ, чем его отсутствие. Поймали
    такое — обязаны вернуть разбор обратно, иначе каждый ответ будет ехать
    через фильтр и терять начало на ожидании тега.
    """
    section("выключатель размышлений, который делает хуже")
    from kuzya_pc import Ollama

    o = Ollama(think=False)
    o.habit["м"] = True
    # Размышления пришли в тексте, отдельного поля не было.
    o.explain("м", split=False, thought=True)
    check("разбор возвращён", o.think, True)
    check("привычка забыта — content теперь чистый", o.habit.get("м"), None)

    # А если Ollama разбирает сама — трогать нечего.
    o = Ollama(think=False)
    o.explain("м", split=True, thought=True)
    check("при чужом разборе не вмешиваемся", o.think, False)

    # И если размышлений нет вовсе — тем более.
    o = Ollama(think=False)
    o.explain("м", split=False, thought=False)
    check("молчаливую модель не трогаем", o.think, False)

    # Разбираемся один раз за запуск, а не на каждом ответе.
    o = Ollama(think=False)
    o.explain("м", split=False, thought=False)
    o.explain("м", split=False, thought=True)
    check("разбираемся однажды", o.think, False)


def test_tool_call() -> None:
    section("вызов инструмента через мост")
    import anthropic

    srv, url = serve(FakeOllama(["сейчас гляну"],
                                [("battery", {"точно": True})]))
    try:
        client = anthropic.Anthropic(api_key="local", base_url=url, max_retries=0)
        with client.messages.stream(
            model="неважно", max_tokens=64,
            messages=[{"role": "user", "content": "сколько заряда"}],
        ) as stream:
            for _ in stream:
                pass
            final = stream.get_final_message()

        kinds = [b.type for b in final.content]
        check("блоки: сначала текст, потом вызов", kinds, ["text", "tool_use"])
        call = final.content[1]
        check("имя инструмента", call.name, "battery")
        check("аргументы разобрались", call.input, {"точно": True})
        check("причина остановки", final.stop_reason, "tool_use")
    finally:
        srv.shutdown()


def test_broken() -> None:
    """Ollama молчит — робот обязан получить честную ошибку, а не пустой ответ.

    Это и есть развилка «уйти в облако»: на месте пустого успешного ответа
    робот сказал бы «что-то пошло не так» и не попробовал бы запасной путь.
    """
    section("Ollama не отвечает")
    import anthropic

    srv, url = serve(Broken([]))
    try:
        client = anthropic.Anthropic(api_key="local", base_url=url, max_retries=0)
        try:
            with client.messages.stream(
                model="неважно", max_tokens=16,
                messages=[{"role": "user", "content": "привет"}],
            ) as stream:
                for _ in stream:
                    pass
            check("должно было упасть", "не упало", "APIStatusError")
        except anthropic.APIStatusError as e:
            check("код ошибки", e.status_code, 502)
        except anthropic.APIConnectionError:
            check("код ошибки", "обрыв связи", 502)
    finally:
        srv.shutdown()


def test_unthink() -> None:
    """Размышления не должны доехать до речи, даже без открывающего тега.

    Qwen3 не пишет <think> в ответе: тег уже стоит в шаблоне запроса, и
    генерация начинается сразу внутри размышлений. На живом роботе это
    вылилось в полторы страницы рассуждений, зачитанных вслух.
    """
    section("размышления не выходят наружу")
    from kuzya_pc import Unthink

    def через(куски: list[str], привычка=None, модель="м") -> str:
        f = Unthink(привычка, модель)
        out = "".join(f.feed(c) for c in куски)
        return out + f.close()

    check("закрывающий тег без открывающего",
          через(["Надо ответить коротко. ", "Пожалуй, так.", "</think>", "Привет!"]),
          "Привет!")
    # Обычную пару не трогаем: перед ней может стоять настоящий текст, и
    # вырезать его нельзя. Пару разберёт фильтр на стороне робота — он умеет
    # это делать не теряя начала, и второго такого разборщика заводить незачем.
    check("обычная пара едет к роботу как есть",
          через(["Сейчас<think>думаю</think>", " гляну."]),
          "Сейчас<think>думаю</think> гляну.")
    check("тег разорван между кусками",
          через(["думаю", "</thi", "nk>", "Готово."]), "Готово.")
    check("короткий ответ без размышлений доходит целиком",
          через(["Привет", ", Игорь!"]), "Привет, Игорь!")
    check("длинный ответ не теряется", через(["а" * 500]), "а" * 500)
    # Оборванный ответ ничего не доказывает: тег мог быть в той части, которая
    # не сгенерировалась. Раньше такой ответ переубеждал фильтр — и следующие
    # размышления ехали прямиком в речь.
    f = Unthink({"м": True}, "м")
    f.feed("думаю без конца")
    check("оборванный ответ не переубеждает", f.close(complete=False), "думаю без конца")
    check("привычка устояла", f.habit.get("м"), True)
    # Порога, после которого фильтр отпускает начало, быть не должно: на живом
    # роботе размышления оказались в пять раз длиннее любого разумного порога.
    ворох = "Надо ответить коротко. Хотя лучше переспросить. " * 30
    check("полторы страницы размышлений отрезаны",
          через([ворох, "</think>", "Привет!"]), "Привет!")

    # Привычка: выяснили один раз — дальше не держим и не платим задержкой.
    привычка: dict[str, bool] = {}
    через(["думаю", "</think>", "Да."], привычка)
    check("болтун запомнен", привычка.get("м"), True)
    привычка = {}
    через(["Привет!"], привычка)
    check("молчун запомнен", привычка.get("м"), False)
    f = Unthink(привычка, "м")
    check("молчуна не придерживаем", f.feed("Привет"), "Привет")
    # Учимся несимметрично: «думает» — доказанный факт, «не думает» — всего
    # лишь отсутствие улики. Один ответ без тега не отменяет увиденного.
    привычка = {"м": True}
    через(["Привет!"], привычка)
    check("один ответ без тега не отменяет доказанного", привычка.get("м"), True)


def test_stt_confidence() -> None:
    """Вместе с текстом наружу едет уверенность распознавания.

    По ней робот решает, можно ли выполнять услышанное. Без неё он однажды
    поехал по фразе «Кузяка идла», которую модель домыслила до «влево».
    """
    section("уверенность распознавания")
    import json
    import urllib.request

    class Сегмент:
        def __init__(self, text, logprob, no_speech=0.0):
            self.text, self.avg_logprob = text, logprob
            self.no_speech_prob = no_speech

    class Модель:
        def transcribe(self, *a, **kw):
            return ([Сегмент(" вперёд на метр", -0.31),
                     Сегмент(" шшш", -1.40, no_speech=0.99)],
                    types.SimpleNamespace(duration=1.5))

    whisper = Whisper("tiny")
    whisper._model = Модель()
    text, sure = whisper.transcribe("звук".encode())
    check("тишину выбросили", text, "вперёд на метр")
    check("уверенность посчитана по оставшемуся", round(sure, 2), -0.31)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.cfg = Config("тест", whisper=whisper, ollama=FakeOllama([]))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/stt"
        req = urllib.request.Request(url, data="звук".encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
        check("текст доехал", body["text"], "вперёд на метр")
        check("уверенность доехала", round(body["sure"], 2), -0.31)
    finally:
        srv.shutdown()


def test_health() -> None:
    section("здоровье")
    import json
    import urllib.request

    srv, url = serve(FakeOllama([]))
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        check("Ollama видна", data["ollama"], True)
        check("модель названа", data["модель"], "тест")
        check("распознавание ещё не грузилось", data["whisper_на"], "не загружена")
    finally:
        srv.shutdown()


def main() -> int:
    # Whisper в проверке не участвует: он про видеокарту, а не про логику.
    for test in (test_messages, test_stream, test_ping, test_whisper_fallback, test_tts, test_warming, test_think_switch,
                 test_tool_call, test_broken,
                 test_unthink, test_stt_confidence, test_health):
        test()
        print("   ...")
    if FAILED:
        print(f"\nРАЗОШЛОСЬ: {len(FAILED)}")
        for item in FAILED:
            print(f"  ✗ {item}")
        return 1
    print("\nВсё сошлось.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
