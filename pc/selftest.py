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
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kuzya_pc                                          # noqa: E402
from kuzya_pc import (Аватар, Config, Handler, Whisper,   # noqa: E402
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
    srv.avatar = Аватар()
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
        VOICES = ("eugene", "baya")
        speaker = "eugene"

        def кто(self, speaker=""):
            return speaker if speaker in self.VOICES else self.speaker

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
            # Робот называет этим именем свой кэш готовых фраз. Без заголовка
            # он звал кэш тем голосом, что записан в его настройках, — и после
            # смены --voice заготовки («Да?», «Не расслышал.») продолжали
            # звучать прежним голосом, а всё остальное новым.
            check("сказали, каким голосом это прозвучало",
                  resp.headers.get("X-Voice"), "baya")
        check("это настоящий wav", body[:4] + body[8:12], b"RIFFWAVE")
        check("фраза и голос доехали", voice.said, ("Привет!", "baya"))

        with ask({"text": "Привет!"}) as resp:
            check("голос не назвали — отвечаем своим",
                  resp.headers.get("X-Voice"), "eugene")

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


def test_voiceprints() -> None:
    """Голоса заводятся сами, из разговора, без обряда знакомства.

    Первая версия просила «скажи три фразы» — на живом роботе это не сработало
    ни разу. Теперь каждая обращённая к роботу фраза либо уточняет известный
    голос, либо заводит новый, пока безымянный. Имя приходит потом, из
    разговора, и безымянный становится Игорем вместе со всем архивом.

    Саму модель здесь не поднимаем — она весит восемьдесят мегабайт и тянет
    torch. Проверяем то, что ломается на самом деле: пороги, усреднение,
    переименование и то, что чужой голос не выдаётся за своего.
    """
    section("узнавание по голосу")
    from kuzya_pc import Voiceprints

    store = Path(tempfile.mkdtemp()) / "голоса.json"
    who = Voiceprints(store)

    # Вместо модели — заранее заданные векторы: так проверяется логика, а не
    # веса нейросети.
    свой, ещё, чужой, третий = b"one", b"two", b"other", b"third"
    # «третий» подобран нарочно в серую зону: он похож на обоих примерно
    # наполовину — выше порога «точно не он», ниже порога «точно он».
    # «третий» подобран нарочно в серую зону: похож на обоих известных
    # примерно поровну — выше порога «точно не он», ниже порога «точно он».
    серо = (Voiceprints.SAME + Voiceprints.NEW) / 2
    сбоку = (1 - 2 * серо ** 2) ** 0.5
    векторы = {свой: [1.0, 0.0, 0.0], ещё: [0.95, 0.1, 0.0],
               чужой: [0.0, 1.0, 0.0], третий: [серо, серо, сбоку]}

    def fake(wav, *a, **kw):
        import numpy as np
        v = np.asarray(векторы[wav], dtype="float32")
        return v / (float(np.linalg.norm(v)) or 1.0)

    who._vector = fake

    # Первая фраза незнакомца слепка НЕ заводит. Одного раза мало: голос
    # уплывает — человек отвернулся, сказал тише, микрофон подавился, — и
    # похожесть падает ниже порога у своего же. Раньше этого хватало, чтобы
    # завести нового, и хозяин дома размножался кличками: за вечер разговора
    # Игорь превратился в «Игорь, голос 1, голос 2, голос 3».
    имя, похожесть, метка = who.identify(свой, seconds=3.0)
    check("первого не узнали — некого", имя, "")
    check("но метку дали", bool(метка), True)
    check("по одной фразе нового не заводим", who.confirm(метка), "")

    # А вот второй фразы подряд, похожей на первую, достаточно.
    имя, похожесть, метка = who.identify(ещё, seconds=3.0)
    check("на второй подряд завели", who.confirm(метка), "голос 1")

    # Третья фраза того же человека: узнали и уточнили слепок.
    имя, похожесть, метка = who.identify(ещё, seconds=3.0)
    check("своего узнали", имя, "голос 1")
    check("и уверенно", похожесть > Voiceprints.SAME, True)
    who.confirm(метка)
    check("слепок уточнён", who.people["голос 1"]["фраз"], 2)

    # Чужой голос: не выдаём за своего и заводим отдельно — по тем же двум
    # фразам подряд.
    имя, похожесть, метка = who.identify(чужой, seconds=3.0)
    check("чужого не выдали за своего", имя, "")
    check("похожесть честная", похожесть < Voiceprints.NEW, True)
    check("и по первой фразе не завели", who.confirm(метка), "")
    имя, похожесть, метка = who.identify(чужой, seconds=3.0)
    check("завели отдельно", who.confirm(метка), "голос 2")

    # Серая зона: похоже, но не точно. Ни приписывать, ни заводить нельзя —
    # первое покажет чужие записи, второе расплодит призраков.
    имя, похожесть, метка = who.identify(третий, seconds=3.0)
    check("в серой зоне не узнаём", имя, "")
    check("и не заводим", who.confirm(метка), "")
    check("голосов по-прежнему двое", len(who.people), 2)

    # Двое РАЗНЫХ незнакомцев подряд в одного не склеиваются: гости в комнате
    # говорят по очереди, и «два раза подряд» без проверки на похожесть
    # завело бы им один общий слепок на двоих.
    who.people.clear()
    who.confirm(who.identify(чужой, seconds=3.0)[2])
    check("разные незнакомцы подряд не склеились",
          who.confirm(who.identify(третий, seconds=3.0)[2]), "")
    check("и никого не завели", len(who.people), 0)

    # Короткая фраза: узнать по ней ещё можно, а заводить нового нельзя.
    who.people.clear()
    имя, _, метка = who.identify(свой, seconds=0.4)
    check("по обрывку нового не заводим", who.confirm(метка), "")

    # Имя из разговора: безымянный становится Игорем вместе с архивом.
    who.people.clear()
    who.confirm(who.identify(свой, seconds=3.0)[2])   # первая — только ждём
    who.confirm(who.identify(ещё, seconds=3.0)[2])    # на второй завёлся
    who.confirm(who.identify(ещё, seconds=3.0)[2])    # третья уточнила
    check("накопили фразы", who.people["голос 1"]["фраз"], 2)
    имя, _, метка = who.identify(ещё, seconds=3.0)
    check("представился", who.confirm(метка, "Игорь"), "Игорь")
    check("кличка исчезла", "голос 1" in who.people, False)
    check("а фразы уцелели", who.people["Игорь"]["фраз"], 3)

    # Голосов не бесконечно, и первыми уходят безымянные с парой фраз.
    for i in range(Voiceprints.LIMIT + 3):
        who.people[f"голос {i + 5}"] = {"вектор": [0.0, 0.0, 1.0], "фраз": 1}
    who._trim()
    check("голосов не больше предела", len(who.people), Voiceprints.LIMIT)
    check("названный уцелел", "Игорь" in who.people, True)

    # Пороги строги, и расплата за это — расщепление: один человек в разных
    # настроениях заводится дважды. Чиним задним числом, по накопленным
    # слепкам: имя побеждает кличку.
    who.people.clear()
    who.people["Игорь"] = {"вектор": [1.0, 0.0, 0.0], "фраз": 9}
    who.people["голос 5"] = {"вектор": [0.9, 0.44, 0.0], "фраз": 2}
    who.people["Настя"] = {"вектор": [0.0, 0.0, 1.0], "фраз": 4}
    who._merge_twins()
    check("близнецы слились", sorted(who.people), ["Игорь", "Настя"])
    check("фразы сложились", who.people["Игорь"]["фраз"], 11)
    check("непохожего не тронули", who.people["Настя"]["фраз"], 4)

    # А теперь то же самое, но слияние случается ПРЯМО В confirm: слепок, в
    # который мы влили фразу, растворяется в другом. На живом роботе это
    # уронило сервер с KeyError ровно в тот момент, когда он впервые всех узнал.
    who.people.clear()
    who.people["голос 1"] = {"вектор": [1.0, 0.0, 0.0], "фраз": 9}
    who.people["голос 4"] = {"вектор": [0.92, 0.39, 0.0], "фраз": 1}
    _, _, метка = who.identify(ещё, seconds=3.0)
    выживший = who.confirm(метка)
    check("после слияния имя настоящее", выживший in who.people, True)
    check("и слепок один", len(who.people), 1)

    # Слепок обязан оставаться подвижным, сколько бы фраз в нём ни лежало.
    # Раньше усреднялось по ВСЕМ фразам сразу, и на живом роботе в слепке
    # Игоря накопилось 162 — новая фраза весила шесть десятых процента, то
    # есть слепок застыл навсегда. Сменили микрофон или переставили робота в
    # другую комнату — и хозяин перестал узнаваться, а починить нечем.
    who.people.clear()
    старый = [1.0, 0.0, 0.0]
    who.people["Игорь"] = {"вектор": list(старый), "фраз": 500}
    who._absorb("Игорь", __import__("numpy").asarray([0.0, 1.0, 0.0],
                                                     dtype="float32"))
    сдвиг = who.people["Игорь"]["вектор"][1]
    check("слепок с полутысячей фраз всё ещё двигается", сдвиг > 0.02, True)
    check("но не переворачивается одной фразой", сдвиг < 0.5, True)
    check("а счётчик фраз честно растёт", who.people["Игорь"]["фраз"], 501)

    who.people.clear()
    who.people["Игорь"] = {"вектор": [1.0, 0.0, 0.0], "фраз": 3}
    who._write()
    check("слепки пережили перезапуск",
          Voiceprints(store).people["Игорь"]["фраз"], 3)
    check("забыли", who.forget("Игорь"), True)
    check("чужого забыть нельзя", who.forget("Никто"), False)


def test_жилец_по_имени_что() -> None:
    """Слепок нельзя завести под словом, которым человека не зовут.

    С живого ПК, прогрев узнавания:

        узнаю по голосу 7: Игорь, голос 1, голос 3, Рома, голос 2, голос 4, Что

    Жильца «Что» завёл телевизор: робот спросил «как тебя зовут?», в комнате
    прозвучало «Что он сказал?», и слово ушло на ПК как имя. Дальше этот слепок
    участвует в КАЖДОМ сравнении и тянет на себя чужие фразы.

    Робот такую проверку уже делает у себя, и это не повод не делать её здесь.
    Имя приходит по сети, от сборки, которую ПК не выбирает: на роботе стоял
    старый образ. А главное — слепки лежат на ПК и переживают любое обновление
    робота, поэтому только отсюда можно убрать УЖЕ заведённого жильца.
    """
    section("жилец по имени «Что»")
    import json

    from kuzya_pc import Voiceprints, годится_в_имя

    check("вопросительное именем не будет", годится_в_имя("Что"), False)
    check("и в любом регистре", годится_в_имя("ЧТО"), False)
    for слово in ("Кто", "Где", "Как", "Я", "Ты", "Это", "Да", "Нет", "Ладно"):
        check(f"«{слово}» — не имя", годится_в_имя(слово), False)
    for имя in ("Игорь", "Рома", "Анна-Мария", "Igor"):
        check(f"«{имя}» — имя", годится_в_имя(имя), True)
    check("кличка безымянного проходит", годится_в_имя("голос 4"), True)
    check("мусор распознавания — нет", годится_в_имя("две2"), False)
    check("одна буква — нет", годится_в_имя("А"), False)
    check("пусто — нет", годится_в_имя(""), False)

    # Уже заведённого жильца выносим при чтении файла: слепки переживают
    # обновление робота, и чинить только будущее тут мало.
    store = Path(tempfile.mkdtemp()) / "голоса.json"
    store.write_text(json.dumps({
        "Игорь": {"вектор": [1.0, 0.0], "фраз": 400},
        "голос 4": {"вектор": [0.0, 1.0], "фраз": 3},
        "Что": {"вектор": [0.7, 0.7], "фраз": 1},
    }, ensure_ascii=False), "utf-8")
    who = Voiceprints(store)
    check("жилец «Что» вынесен", "Что" in who.people, False)
    check("Игорь на месте", who.people["Игорь"]["фраз"], 400)
    check("безымянного не тронули", "голос 4" in who.people, True)
    # И на диске тоже: иначе он вернётся при следующем запуске.
    check("и на диске его больше нет",
          "Что" in json.loads(store.read_text("utf-8")), False)

    # Имя, пришедшее по сети от старого робота, слепок не заводит. Голос при
    # этом обрабатывается дальше как безымянный — человек-то говорил.
    def fake(wav, *a, **kw):
        import numpy as np
        v = np.asarray([1.0, 0.0], dtype="float32")
        return v / float(np.linalg.norm(v))

    who._vector = fake
    имя, _, метка = who.identify(b"wav", seconds=3.0)
    check("узнали Игоря", имя, "Игорь")
    check("но именем «Что» не переименовали", who.confirm(метка, "Что"), "Игорь")
    check("и такого слепка не завелось", "Что" in who.people, False)


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


def test_model_choice() -> None:
    """Рассуждающую модель подменяем нерассуждающей — но только на скачанную.

    Размышления наружу не идут: их режет фильтр по дороге к речи. А время на
    них тратится целиком, и всё это время робот молчит — на живом роботе «Да,
    я здесь!» стоило 695 токенов и девяти секунд, из которых восемь ушли в
    никуда. Выключить это у гибридной модели нечем: три способа перепробованы
    и все три записаны в kuzya_pc.py как неудачи.

    Но подменять можно только на то, что уже есть на диске. Предложить
    несуществующую модель — значит сломать разговор целиком ради скорости, а
    неотвечающий робот хуже медленного.
    """
    section("выбор модели: не думать вслух")
    from kuzya_pc import выбрать_модель, рассуждает

    check("гибрид qwen3 думает", рассуждает("qwen3:4b"), True)
    # Одно слово в имени, а поведение противоположно. Спутать их — значит
    # либо не лечить медлительность, либо лечить несуществующую.
    check("instruct-сборка не думает", рассуждает("qwen3:4b-instruct-2507"), False)
    check("deepseek-r1 думает", рассуждает("deepseek-r1:7b"), True)
    check("обычная модель не думает", рассуждает("llama3.1:8b"), False)

    # Замена скачана — берём молча, но говорим почему.
    имя, слова = выбрать_модель(
        "qwen3:4b", ["qwen3:4b", "qwen3:4b-instruct-2507-q4_K_M", "llama3.1:8b"])
    check("взяли нерассуждающую", имя, "qwen3:4b-instruct-2507-q4_K_M")
    check("и объяснили почему", "думает вслух" in слова, True)

    # Скачано другое квантование той же сборки — подходит. Заставлять качать
    # ровно нашу было бы придирками.
    имя, _ = выбрать_модель("qwen3:4b", ["qwen3:4b", "qwen3:4b-instruct-q8_0"])
    check("чужое квантование тоже годится", имя, "qwen3:4b-instruct-q8_0")

    # Замены нет — работаем как работали и называем ОДНУ команду.
    имя, слова = выбрать_модель("qwen3:4b", ["qwen3:4b"])
    check("без замены модель не меняем", имя, "qwen3:4b")
    check("но команду называем", "ollama pull qwen3:4b-instruct-2507-q4_K_M" in слова, True)

    # Нерассуждающую не трогаем и молчим о ней.
    имя, слова = выбрать_модель("qwen3:4b-instruct-2507", ["qwen3:4b-instruct-2507"])
    check("молчаливую не трогаем", (имя, слова), ("qwen3:4b-instruct-2507", ""))

    # Чужое семейство не подсовываем: llama вместо qwen — это другой робот.
    имя, _ = выбрать_модель("qwen3:4b", ["qwen3:4b", "llama3.1:8b-instruct"])
    check("чужое семейство не берём", имя, "qwen3:4b")


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


def test_context_window() -> None:
    """Запрос робота обязан помещаться в окно, которое просит мост.

    Умолчание Ollama — 4096 токенов. Постоянная часть запроса робота, то есть
    системный промпт плюс схемы инструментов, это около двенадцати тысяч
    символов, и половина из них кириллица — а она в токенизаторе дороже
    латиницы. Даже по самой щедрой оценке (четыре символа на токен) в 4096 это
    не помещается вместе с историей и ответом.

    За краем окна Ollama молча выбрасывает начало — ровно ту часть, где
    записано, кто робот такой и как ему разговаривать. Ни ошибки, ни
    предупреждения: робот просто начинает отвечать казённо и забывать разговор.
    """
    section("окно контекста")
    from kuzya_pc import DEFAULT_CTX, Ollama

    мост = Ollama("http://нет")
    check("num_ctx уходит в Ollama явно",
          мост._payload("м", [], [], 384)["options"].get("num_ctx"), DEFAULT_CTX)
    check("и он больше умолчания самой Ollama", DEFAULT_CTX > 4096, True)
    # Прогрев обязан просить то же окно: Ollama держит модель вместе с
    # KV-кэшем нужного размера, и запрос с другим num_ctx перезагружает всё
    # заново — то есть греет не то и не экономит ничего.
    исходник = (Path(__file__).resolve().parent / "kuzya_pc.py").read_text(encoding="utf-8")
    кусок = исходник[исходник.index("def warm(self, model"):]
    check("прогрев просит то же окно", "self._options(" in кусок[:1200], True)
    check("совсем маленькое окно не примем", Ollama("http://нет", ctx=128).ctx >= 2048, True)

    # Настоящий выключатель размышлений — chat_template_kwargs, а не think:
    # think у Ollama означает «разбирать ли размышления отдельным полем», и
    # модель при нём думает ровно столько же. Гибридным моделям (Qwen3.5 и
    # родня) при пределе ответа в 384 токена это стоит всей фразы.
    выкл = мост._payload("м", [], [], 384)
    check("размышления выключены аргументом шаблона",
          выкл.get("chat_template_kwargs"), {"enable_thinking": False})
    вкл = Ollama("http://нет", think=True)._payload("м", [], [], 384)
    check("а с --think их не выключаем", "chat_template_kwargs" in вкл, False)

    # Сборка Ollama может не знать ни того, ни другого параметра. Тогда мост
    # гасит их по одному, и заходов должно хватить на оба плюс рабочий.
    кусок = исходник[исходник.index("def chat(self, model"):]
    check("заходов хватает на оба необязательных параметра",
          "range(3)" in кусок[:900], True)
    мост._kwargs_known = False
    check("без chat_template_kwargs запрос всё равно собирается",
          "chat_template_kwargs" in мост._payload("м", [], [], 384), False)


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


def test_gigaam() -> None:
    """GigaAM живёт рядом с Whisper и по тому же договору.

    Он точнее на русском — семьсот тысяч часов против доли русского у Whisper,
    и на коротких редких словах вроде имени робота это заметно. Но уверенности
    он не отдаёт, и это не мелочь: по ней робот решает, можно ли по фразе
    ехать. Поэтому выбор за человеком, а по умолчанию остаётся Whisper.
    """
    section("распознавание GigaAM")
    g = kuzya_pc.GigaAM()
    check("договор тот же, что у Whisper",
          (hasattr(g, "transcribe"), hasattr(g, "warm"), hasattr(g, "size")),
          (True, True, True))
    check("модель по умолчанию — третья версия", g.size.startswith("v3"), True)
    check("до прогрева ничего не грузит", g._model, None)

    # Уверенности нет — и это должно быть сказано честно, а не выдумано числом.
    исходник = (Path(__file__).resolve().parent / "kuzya_pc.py"
                ).read_text(encoding="utf-8")
    кусок = исходник[исходник.index("class GigaAM"):исходник.index("class Voiceprints")]
    check("возвращает None вместо выдуманной уверенности",
          "return text, None" in кусок, True)
    # По умолчанию — Whisper: молча менять распознаватель нельзя.
    check("по умолчанию остаётся whisper",
          '"--stt", default="whisper"' in исходник, True)
    check("и если GigaAM не встанет — не оглохнем",
          "распознаю Whisper" in исходник, True)
    # Шапка при запуске обязана называть того, кто и правда слушает. Стояло имя
    # из командной строки, и она сообщала про Whisper, когда работал GigaAM —
    # то есть врала ровно там, где человек проверяет, что всё завелось.
    check("шапка берёт имя у настоящего распознавателя",
          "cfg.whisper.size.rsplit" in исходник, True)
    check("а не у аргумента командной строки",
          "args.whisper.rsplit" in исходник, False)

    # Без ffmpeg GigaAM не прочитает ни одного файла: своего декодера у него
    # нет. Узнать об этом надо при запуске, а не на первой фразе — иначе робот
    # встречает человека трассировкой, и так на каждое слово.
    # GigaAM отдаёт не строку, а объект с полем text. Робот на этом падал на
    # каждой фразе: 'TranscriptionResult' object has no attribute 'strip'.
    # Полагаться на одно имя поля нельзя — библиотека молодая.
    import types as _t
    for ответ, ждём, имя in (
        (_t.SimpleNamespace(text="Кузя, вперёд"), "Кузя, вперёд", "поле text"),
        (_t.SimpleNamespace(transcription="привет"), "привет", "поле transcription"),
        ("уже строка", "уже строка", "просто строка"),
        (None, "", "ничего"),
        (_t.SimpleNamespace(text="  с пробелами  "), "с пробелами", "обрезка"),
    ):
        check(f"текст из ответа: {имя}", kuzya_pc._текстом(ответ), ждём)

    check("проверяем ffmpeg до первой фразы",
          hasattr(kuzya_pc.GigaAM, "ffmpeg_есть"), True)
    check("и говорим, чем лечится", "winget install" in исходник, True)
    было = kuzya_pc.shutil.which
    try:
        kuzya_pc.shutil.which = lambda имя: None
        check("без ffmpeg честно отказывается",
              kuzya_pc.GigaAM.ffmpeg_есть(), False)
        # Сам gigaam здесь не установлен, поэтому смотрим в исходник: заслон
        # должен стоять в _load до обращения к библиотеке — иначе он проверит
        # ffmpeg уже после того, как модель загрузится, то есть слишком поздно.
        кусок = исходник[исходник.index("class GigaAM"):
                         исходник.index("class Voiceprints")]
        загрузка = кусок[кусок.index("def _load"):]
        check("заслон стоит в загрузке модели",
              "ffmpeg_есть()" in загрузка, True)
        check("и отказ объясняет причину",
              "нет ffmpeg" in загрузка, True)
    finally:
        kuzya_pc.shutil.which = было


def test_no_initial_prompt() -> None:
    """Подсказки распознаванию быть не должно, и это проверяется.

    Её уже пробовали и убрали: на шумной или тихой записи Whisper начинает
    повторять слова из подсказки, разгоняя генерацию до предела, и секунда
    звука разбирается полминуты. Написано в voice/robot_voice/stt.py, а
    проверки не было — и подсказку вернули заново, не заметив. Теперь не выйдет.
    """
    section("подсказки распознаванию нет")
    исходник = (Path(__file__).resolve().parent / "kuzya_pc.py"
                ).read_text(encoding="utf-8")
    живой = [с for с in исходник.splitlines()
             if "initial_prompt" in с and not с.strip().startswith("#")]
    check("initial_prompt не передаётся в модель", живой, [])
    check("и почему — записано рядом",
          "initial_prompt" in исходник, True)


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


def test_avatar() -> None:
    """/avatar/state — приём того же состояния, что и на роботе.

    Отрисовка — дело браузера, но РЕШЕНИЕ (рот, наклон, метка) принимает
    ровно та же логика, что рисует лицо на самом роботе (face/character.py),
    просто вызванная отсюда. Здесь проверяем, что она вообще позвана и не
    подменена своей — а заодно что чужой файл с диска через «..» не отдать.
    """
    section("аватар на ПК")
    import http.client
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    srv, url = serve(FakeOllama([]))
    try:
        # «/avatar» без хвостового «/» — редирект, а не страница напрямую:
        # иначе все относительные пути внутри неё (config.json, state,
        # сама модель) уезжают на уровень выше и тихо получают чужой JSON
        # вместо настоящих настроек — так и было на живом ПК.
        адрес = urlsplit(url)
        соединение = http.client.HTTPConnection(адрес.hostname, адрес.port, timeout=5)
        соединение.request("GET", "/avatar")
        ответ = соединение.getresponse()
        ответ.read()
        check("«/avatar» без слэша — редирект, а не страница напрямую",
              ответ.status, 301)
        check("редирект ведёт на слэш на конце",
              ответ.getheader("Location"), "/avatar/")
        соединение.close()

        тело = json.dumps({"эмоция": "рад", "говорит": "привет",
                           "музыка": {"играет": False}}).encode("utf-8")
        запрос = urllib.request.Request(url + "/avatar/state", data=тело,
                                        method="POST")
        with urllib.request.urlopen(запрос, timeout=5) as r:
            check("POST принят", json.loads(r.read())["ok"], True)

        with urllib.request.urlopen(url + "/avatar/state", timeout=5) as r:
            данные = json.loads(r.read().decode("utf-8"))
        check("сырое состояние вернулось как было", данные["эмоция"], "рад")
        check("а поза — уже посчитана той же логикой, что и на экране "
              "робота (говорит → рот приоткрыт хоть иногда)",
              "рот" in данные.get("поза", {}), True)

        # Модель (.model3.json) каждый кладёт сам — см. pc/avatar/README.md;
        # пока её нет, отсутствующий файл обязан дать понятную ошибку, а не
        # тихо промолчать пустым экраном без единого объяснения.
        try:
            urllib.request.urlopen(url + "/avatar/model/nonexistent.model3.json",
                                   timeout=5)
            check("нет файла модели — сервер должен был отказать", False, True)
        except urllib.error.HTTPError as e:
            check("нет файла — понятная ошибка, а не пустой экран",
                  e.code, 404)

        # «..» не должен вывести за пределы папки аватара — иначе через этот
        # путь можно попросить любой файл с диска ПК. Проверяем на РЕАЛЬНО
        # существующем файле снаружи (pc/kuzya_pc.py, один уровень вверх от
        # pc/avatar/): если защиту сломать, здесь будет 200 с его текстом, а
        # не 404 — 404 сам по себе ничего не доказывает, файла могло просто
        # не быть по неверно посчитанному пути.
        try:
            urllib.request.urlopen(url + "/avatar/../kuzya_pc.py", timeout=5)
            check("побег из папки аватара через «..» должен быть отвергнут",
                  False, True)
        except urllib.error.HTTPError as e:
            check("«..» не выпускает из папки аватара", e.code, 404)
    finally:
        srv.shutdown()


def main() -> int:
    # Whisper в проверке не участвует: он про видеокарту, а не про логику.
    for test in (test_messages, test_stream, test_ping, test_whisper_fallback, test_tts, test_voiceprints, test_жилец_по_имени_что, test_warming, test_think_switch, test_model_choice,
                 test_tool_call, test_broken,
                 test_context_window, test_unthink, test_gigaam, test_no_initial_prompt, test_stt_confidence, test_health, test_avatar):
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
