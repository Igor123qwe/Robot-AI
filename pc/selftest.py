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
from kuzya_pc import (Аватар, Config, Handler, Съёмка, Whisper,   # noqa: E402
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

        # Своё — в config.local.json поверх общего config.json: путь к модели
        # у каждого свой, а правка общего файла ломала git pull на живом ПК.
        местный = Handler.АВАТАР_ПАПКА / "config.local.json"
        было = местный.read_text(encoding="utf-8") if местный.exists() else None
        try:
            # Своя модель, которая ЕСТЬ на диске (берём вторую готовую), —
            # сильнее общей: так и задумано.
            местный.write_text(json.dumps({"модель": "samples/mao/Mao.model3.json",
                                           "параметры": {"рот": "MyMouth"}},
                                          ensure_ascii=False), encoding="utf-8")
            with urllib.request.urlopen(url + "/avatar/config.json", timeout=5) as r:
                настройки = json.loads(r.read().decode("utf-8"))
            check("config.local.json поверх config.json: свой путь к модели",
                  настройки["модель"], "samples/mao/Mao.model3.json")
            check("…словари дополняются, а не заменяются целиком",
                  (настройки["параметры"]["рот"], настройки["параметры"]["взгляд_x"]),
                  ("MyMouth", "ParamAngleX"))
            # А вот путь к модели, которой на диске НЕТ (старый config.local
            # с прошлых времён: модель переставили, переименовали, не
            # докачали) — раньше подменялся общей моделью из config.json
            # (Shizuku), и в этом была ловушка похуже пустого экрана: человек,
            # который вовсе не просил Live2D, получал на экране робота чужого
            # персонажа молча. Общей модели по умолчанию теперь НЕТ (см. ниже:
            # «аватар не настроен» — робот рисует Кузю сам), и подменять
            # нечем — путь пользователя остаётся как есть, чтобы страница
            # честно показала ошибку про НЕГО, а не нарисовала кого-то чужого.
            жалобы: list = []
            старый_warning = kuzya_pc.log.warning
            kuzya_pc.log.warning = lambda ф, *а: жалобы.append(ф % а if а else ф)
            try:
                местный.write_text(json.dumps({"модель": "model/её-нет.model3.json"},
                                              ensure_ascii=False), encoding="utf-8")
                with urllib.request.urlopen(url + "/avatar/config.json", timeout=5) as r:
                    настройки2 = json.loads(r.read().decode("utf-8"))
            finally:
                kuzya_pc.log.warning = старый_warning
            check("модели из config.local.json нет на диске, общей по умолчанию тоже нет — "
                  "путь пользователя остаётся, чужая модель не подставляется",
                  настройки2["модель"], "model/её-нет.model3.json")
            check("…и жалуемся в лог, чтобы это можно было понять, а не гадать",
                  any("её-нет" in ж and "тоже не на месте" in ж for ж in жалобы), True)
            # Путь наружу из avatar/ существовать может, а страница его всё
            # равно не получит (сервер наружу не отдаёт) — считаем «нет».
            жалобы.clear()
            kuzya_pc.log.warning = lambda ф, *а: жалобы.append(ф % а if а else ф)
            try:
                местный.write_text(json.dumps({"модель": "../../README.md"},
                                              ensure_ascii=False), encoding="utf-8")
                with urllib.request.urlopen(url + "/avatar/config.json", timeout=5) as r:
                    настройки3 = json.loads(r.read().decode("utf-8"))
            finally:
                kuzya_pc.log.warning = старый_warning
            check("путь наружу из avatar/ — это «нет модели» на уровне отдачи файла, "
                  "хотя в config.json он и остаётся строкой как есть",
                  настройки3["модель"], "../../README.md")
            # Общей модели тоже нет (снесли samples/): подменять битый путь
            # ДРУГИМ битым — худшее из возможного, человек будет искать
            # опечатку там, где её нет. Оставляем его путь, страница назовёт
            # именно его. Зовём напрямую: config.json в репозитории трогать
            # нельзя, а случай проверить надо.
            ручка = object.__new__(Handler)          # без сокетов: нужны только методы
            местный.write_text(json.dumps({"модель": "model/её-нет.model3.json"},
                                          ensure_ascii=False), encoding="utf-8")
            жалобы.clear()
            kuzya_pc.log.warning = lambda ф, *а: жалобы.append(ф % а if а else ф)
            try:
                вышло = json.loads(ручка._настройки_с_местными(
                    json.dumps({"модель": "samples/тоже-нет.model3.json"}).encode()).decode())
            finally:
                kuzya_pc.log.warning = старый_warning
            check("нет ни своей модели, ни общей — оставляем ПУТЬ ПОЛЬЗОВАТЕЛЯ, он в ошибке страницы",
                  вышло["модель"], "model/её-нет.model3.json")
            check("…и говорим в лог, что не на месте обе",
                  any("тоже не на месте" in ж for ж in жалобы), True)
        finally:
            if было is None:
                местный.unlink(missing_ok=True)
            else:
                местный.write_text(было, encoding="utf-8")

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
        # Закодированный «..» (%2e%2e) — та же дверь, только с другой ручки:
        # путь раскодируется ДО проверки, а не после.
        try:
            urllib.request.urlopen(url + "/avatar/%2e%2e/kuzya_pc.py", timeout=5)
            check("закодированный «..» тоже должен быть отвергнут", False, True)
        except urllib.error.HTTPError as e:
            check("«%2e%2e» не выпускает из папки аватара", e.code, 404)

        # Файлы модели с пробелом и кириллицей в имени: браузер кодирует их в
        # %20 и %D0…, и без раскодирования пути сервер отвечал 404 на файл,
        # который лежит на месте. И тип картинки — image/jpeg, а не
        # octet-stream: иначе браузер её не показывает, а предлагает скачать.
        картинка = Handler.АВАТАР_ПАПКА / "проба тест.jpg"
        try:
            картинка.write_bytes(b"\xff\xd8proba\xff\xd9")
            from urllib.parse import quote
            try:
                with urllib.request.urlopen(url + "/avatar/" + quote("проба тест.jpg"), timeout=5) as r:
                    check("имя файла с пробелом и кириллицей раскодируется — файл отдан",
                          r.read(), b"\xff\xd8proba\xff\xd9")
                    check("…и с типом картинки, а не octet-stream",
                          r.getheader("Content-Type"), "image/jpeg")
            except urllib.error.HTTPError as e:
                check("имя файла с пробелом и кириллицей раскодируется — файл отдан",
                      f"HTTP {e.code}", "200")
        finally:
            картинка.unlink(missing_ok=True)
    finally:
        srv.shutdown()


def test_avatar_stream() -> None:
    """/avatar/stream — кадры аватара роботу на экран, без браузера.

    Сам headless-браузер здесь не поднимается: он про Playwright и Chrome, а
    не про логику. Кадры кладём в Съёмку руками и проверяем ТО, что робот на
    том конце увидит по HTTP: первый кадр, повтор при тишине (иначе робот
    решит, что ПК пропал, и уйдёт в своё лицо), новый кадр, размер экрана.
    """
    section("поток аватара на экран робота")
    import http.client
    import json
    import threading
    import time
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    ч = [0.0]
    srv, url = serve(FakeOllama([]))
    адрес = urlsplit(url)
    первый_кадр_было = Handler.ПЕРВЫЙ_КАДР
    Handler.ПЕРВЫЙ_КАДР = 0.3            # ждать двадцать секунд тут незачем
    try:
        def спросить(путь: str) -> tuple[int, bytes]:
            с = http.client.HTTPConnection(адрес.hostname, адрес.port, timeout=5)
            с.request("GET", путь)
            ответ = с.getresponse()
            тело = ответ.read()
            с.close()
            return ответ.status, тело

        # Без Playwright съёмки нет — и робот должен узнать об этом сразу,
        # словами, а не висеть на пустом ответе.
        код, тело = спросить("/avatar/stream")
        check("нет съёмки — 503, а не молчание", код, 503)
        check("…и сказано, чего не хватает",
              "playwright" in тело.decode("utf-8", "replace").lower(), True)

        съёмка = Съёмка("")
        srv.avatar.съёмка = съёмка
        check("до первого запроса спроса нет — браузер не крутится впустую",
              съёмка.спрос_есть(), False)

        # Кадров ещё нет (браузер, допустим, только поднимается).
        код, _ = спросить("/avatar/frame.jpg")
        check("кадров ещё нет — 503", код, 503)
        check("запрос робота — это спрос: теперь снимать надо",
              съёмка.спрос_есть(), True)

        первый = b"\xff\xd8first-frame\xff\xd9"
        съёмка.положить(первый)
        with urllib.request.urlopen(url + "/avatar/frame.jpg?w=1024&h=600",
                                    timeout=5) as r:
            check("frame.jpg — последний кадр как есть", r.read(), первый)
            check("…с типом картинки", r.getheader("Content-Type"), "image/jpeg")
        check("размер экрана робота из запроса — размер съёмки",
              съёмка.размер, (1024, 600))

        # Поток. Читаем в своём потоке: сервер отдаёт его бесконечно.
        части: list[bytes] = []
        готово = threading.Event()

        def читать() -> None:
            с = http.client.HTTPConnection(адрес.hostname, адрес.port, timeout=10)
            с.request("GET", "/avatar/stream")
            ответ = с.getresponse()
            части.append(ответ.getheader("Content-Type", "").encode())
            буфер = b""
            while len(части) < 5:
                кусок = ответ.read1(65536)
                if not кусок:
                    break
                буфер += кусок
                while True:
                    н = буфер.find(b"\r\n\r\n")
                    if н < 0:
                        break
                    длина = int([з for з in буфер[:н].split(b"\r\n")
                                 if з.lower().startswith(b"content-length")][0].split(b":")[1])
                    if len(буфер) < н + 4 + длина:
                        break
                    части.append(буфер[н + 4:н + 4 + длина])
                    буфер = буфер[н + 4 + длина:]
            с.close()
            готово.set()

        threading.Thread(target=читать, daemon=True).start()
        # Новых кадров не кладём: первые части обязаны быть ПОВТОРОМ первого —
        # страница без движения (или с застывшей моделью) не повод для
        # робота считать ПК пропавшим.
        срок = time.monotonic() + 5
        while len(части) < 3 and time.monotonic() < срок:
            time.sleep(0.05)
        второй = b"\xff\xd8second-frame\xff\xd9"
        съёмка.положить(второй)
        готово.wait(5)
        # По индексам через get: при сломанном пульсе поток обрывается после
        # первого кадра, и частей меньше — это должно стать несходимостью
        # с именем, а не IndexError без имени.
        часть = lambda н: части[н] if len(части) > н else None   # noqa: E731
        check("поток — multipart/x-mixed-replace",
              (часть(0) or b"").startswith(b"multipart/x-mixed-replace"), True)
        check("первая часть — первый кадр", часть(1), первый)
        check("без новых кадров поток повторяет прежний (пульс), а не молчит",
              часть(2), первый)
        check("новый кадр доехал", второй in части[3:], True)

        # Браузер упал. Раньше кадр с прошлой жизни браузера отдавался
        # мгновенно, поток обрывался на нём (беда), робот через пять секунд
        # приходил снова — и так минуту, до новой попытки с браузером: лицо
        # прыгало между «аватар с ПК» и «рисую сам». Теперь: беда — 503
        # сразу; кадр старше СВЕЖИЙ — 503; остановка браузера сбрасывает кадр.
        # Поток читаем ТОЛЬКО до статуса: при сломанной защите сюда придёт
        # 200 с бесконечным MJPEG, и read() всего тела не вернулся бы никогда.
        def статус(путь: str) -> tuple[int, bytes]:
            с = http.client.HTTPConnection(адрес.hostname, адрес.port, timeout=5)
            с.request("GET", путь)
            ответ = с.getresponse()
            тело = ответ.read() if ответ.status != 200 else b""
            с.close()
            return ответ.status, тело

        съёмка.положить(b"\xff\xd8third-frame\xff\xd9")
        съёмка.беда = "браузер не снялся: упал"
        код, тело = статус("/avatar/stream")
        check("браузер упал — 503 сразу, а не застывший кадр с прошлой жизни",
              (код, "упал" in тело.decode("utf-8", "replace")), (503, True))
        съёмка.беда = ""
        съёмка.когда -= Съёмка.СВЕЖИЙ + 1.0
        код, _ = статус("/avatar/stream")
        check("кадр старше двух секунд первым в поток не отдаётся — 503 (ждём свежего)",
              код, 503)
        код, _ = статус("/avatar/frame.jpg")
        check("…и frame.jpg такой кадр не отдаёт", код, 503)
        номер_был = съёмка.номер
        съёмка.сбросить()
        check("остановка браузера сбрасывает кадр, номер остаётся монотонным",
              (съёмка.кадр(0), съёмка.номер), ((b"", 0), номер_был))
        съёмка.положить(b"\xff\xd8fresh\xff\xd9")
        check("новый кадр после сброса — снова годится",
              съёмка.кадр(0, не_старше=Съёмка.СВЕЖИЙ)[0], b"\xff\xd8fresh\xff\xd9")

        # Робот перестал читать (выключили питание) — обработчик обязан
        # отпустить соединение по таймауту, а не держать его (и headless-
        # браузер) до тайм-аута TCP. Клиент открывает поток и не читает;
        # кадр большой, чтобы буферы сокета переполнились быстро.
        таймаут_было = Handler.ПОТОК_ТАЙМАУТ
        Handler.ПОТОК_ТАЙМАУТ = 0.5
        try:
            съёмка.положить(b"\xff\xd8" + b"x" * (16 * 1024 * 1024) + b"\xff\xd9")
            import socket as _socket
            гнездо = _socket.create_connection((адрес.hostname, адрес.port), timeout=10)
            гнездо.sendall(b"GET /avatar/stream HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(3.0)                     # не читаем: буферы забиты
            гнездо.settimeout(10)
            конец = False
            t0 = time.monotonic()
            while time.monotonic() - t0 < 10:
                try:
                    if not гнездо.recv(1 << 20):
                        конец = True
                        break
                except OSError:
                    break
            гнездо.close()
            check("робот перестал читать поток — сервер отпустил соединение по "
                  "таймауту, а не держит вечно", конец, True)
        finally:
            Handler.ПОТОК_ТАЙМАУТ = таймаут_было
    finally:
        Handler.ПЕРВЫЙ_КАДР = первый_кадр_было
        srv.shutdown()

    # Робот замолчал — аватар спит, а не держит последнее живое лицо.
    аватар = Аватар(часы=lambda: ч[0])
    аватар.принять({"эмоция": "рад", "батарея": 11.3})
    check("свежее состояние — как прислали", аватар.состояние()["эмоция"], "рад")
    ч[0] += Аватар.СТАРЕЕТ + 1
    с = аватар.состояние()
    check("робот замолчал — аватар спит, а не застывает радостным",
          (с["эмоция"], с["поза"]["метка"]), ("сплю", "спит"))
    check("батарея сон переживает", с["батарея"], 11.3)


def test_avatar_reactions() -> None:
    """Чего робот не присылает, а персонаж всё-таки делает: рот по звуку и
    настроение по тексту ответа.

    Рот: синус Питомца — имитация; настоящий звук рождается на этом же ПК,
    и рот обязан идти по его громкости. Настроение: «рад»/«огорчён» робот
    не ставит никогда (методы есть, вызовов нет), единственный источник —
    текст ответа модели.
    """
    section("реакции аватара: рот по звуку, настроение по ответу")
    import json
    import math
    import time
    import urllib.request

    from kuzya_pc import Губы, настроение_по_тексту

    # --- огибающая: тишина — ноль, звук — открыт ----------------------------
    rate = 24000
    тишина = b"\x00\x00" * int(rate * 0.2)
    тон = b"".join(int(12000 * math.sin(i / 10)).to_bytes(2, "little", signed=True)
                   for i in range(int(rate * 0.3)))
    pcm = тишина + тон + тишина
    ч = [10.0]
    губы = Губы(часы=lambda: ч[0])
    check("звука нет — рот не наш (None), пусть решает синус", губы.рот(), None)
    губы.добавить(pcm, rate)
    check("звук синтезирован, но до динамика ещё не дошёл — рот закрыт",
          губы.рот(), 0.0)
    ч[0] = 10.0 + Губы.ЗАДЕРЖКА + 0.1
    check("в тишине перед фразой рот закрыт", губы.рот(), 0.0)
    ч[0] = 10.0 + Губы.ЗАДЕРЖКА + 0.35
    check("на звуке рот открыт", (губы.рот() or 0) > 0.7, True)
    ч[0] = 10.0 + Губы.ЗАДЕРЖКА + 0.6
    check("в тишине в конце фразы — закрыт", губы.рот(), 0.0)
    ч[0] = 10.0 + Губы.ЗАДЕРЖКА + 5.0
    check("фраза отзвучала — снова не наш", губы.рот(), None)
    # Две фразы подряд встают в очередь, как в динамике робота, а не поверх.
    ч[0] = 20.0
    губы.добавить(тон, rate)
    губы.добавить(тон, rate)
    ч[0] = 20.0 + Губы.ЗАДЕРЖКА + 0.3 + 0.1
    check("вторая фраза звучит после первой, а не одновременно",
          (губы.рот() or 0) > 0.7, True)

    # --- настроение по тексту -----------------------------------------------
    check("«к сожалению, не могу» — огорчён",
          настроение_по_тексту("К сожалению, я не могу это сделать."), "огорчён")
    check("«отлично, сделано!» — рад", настроение_по_тексту("Отлично, сделано!"), "рад")
    check("сухой ответ — без настроения",
          настроение_по_тексту("Сейчас двадцать один градус."), "")
    check("пусто — пусто", настроение_по_тексту(""), "")

    # --- настроение по слову модели ------------------------------------------
    from kuzya_pc import настроение_по_слову
    check("вердикт модели: «Рад.» / «огорчён» / «Спокоен» — как есть, в любом виде",
          [настроение_по_слову(о) for о in ("Рад.", "Он огорчён", "Спокоен", "грустно")],
          ["рад", "огорчён", "спокоен", "огорчён"])
    check("невнятный вердикт — пусто, слова остаются", настроение_по_слову("фиолетовый"), "")

    # Модель уточняет настроение там, где слова его не видят, — и наоборот,
    # её «спокоен» гасит настроение, которое слова придумали по одному
    # «отлично». Ответ модели приходит потом, в своём потоке; пока его нет,
    # держится вердикт по словам.
    спросили: list[str] = []
    ч[0] = 25.0
    ав = Аватар(часы=lambda: ч[0])
    ав.спросить_настроение = lambda текст: (спросили.append(текст), "Рад")[1]
    ав.принять({"эмоция": "спокоен", "говорит": "Сейчас двадцать один градус."})
    ав.ответил("Сейчас двадцать один градус.")
    for _ in range(100):
        if ав.состояние()["эмоция"] == "рад":
            break
        time.sleep(0.02)
    check("слова не видят настроения — модель спросили, и её «рад» взят",
          (спросили, ав.состояние()["эмоция"]), (["Сейчас двадцать один градус."], "рад"))
    ав.спросить_настроение = lambda текст: "спокоен"
    ав.ответил("Отлично, сделано!")
    for _ in range(100):
        if ав.состояние()["эмоция"] == "спокоен":
            break
        time.sleep(0.02)
    check("модель говорит «спокоен» — вердикт слов («рад» по «отлично») снят",
          ав.состояние()["эмоция"], "спокоен")
    ав.спросить_настроение = lambda текст: 1 / 0
    ав.ответил("К сожалению, не вышло.")
    time.sleep(0.1)
    check("модель не ответила — остаётся вердикт по словам",
          ав.состояние()["эмоция"], "огорчён")
    ав.спросить_настроение = None
    ав.ответил("")
    check("пустой ответ модель не спрашивают", len(спросили), 1)

    # --- слияние в состоянии -----------------------------------------------
    аватар = Аватар(часы=lambda: ч[0])
    ч[0] = 30.0
    аватар.принять({"эмоция": "спокоен", "говорит": "Отлично, сделано!"})
    аватар.ответил("Отлично, сделано!")
    check("ответ радостный — персонаж рад, хотя робот прислал «спокоен»",
          аватар.состояние()["эмоция"], "рад")
    аватар.принять({"эмоция": "встревожен", "говорит": ""})
    check("тревогу робота настроение ответа не перебивает",
          аватар.состояние()["эмоция"], "встревожен")
    ч[0] = 30.0 + 60.0
    аватар.принять({"эмоция": "спокоен", "говорит": ""})
    check("настроение гаснет само — застывшее лицо запрещено",
          аватар.состояние()["эмоция"], "спокоен")

    ч[0] = 40.0
    аватар.принять({"эмоция": "спокоен", "говорит": "привет"})
    аватар.озвучил(pcm, rate)
    ч[0] = 40.0 + Губы.ЗАДЕРЖКА + 0.35
    с = аватар.состояние()
    check("говорит и звук есть — рот по звуку, не синус",
          (с["поза"].get("рот_по"), с["поза"]["рот"] > 0.7), ("звуку", True))
    аватар.принять({"эмоция": "спокоен", "говорит": ""})
    с = аватар.состояние()
    check("робот перебит (говорит пусто) — рот закрыт, огибающая не доигрывает",
          (с["поза"].get("рот_по"), с["поза"]["рот"]), (None, 0.0))

    # --- ответ модели через мост доезжает до аватара ------------------------
    srv, url = serve(FakeOllama(["К сожалению", ", не получилось."]))
    try:
        запрос = urllib.request.Request(
            url + "/v1/messages",
            data=json.dumps({"model": "x", "max_tokens": 64,
                             "messages": [{"role": "user", "content": "сделай"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(запрос, timeout=10) as r:
            r.read()
        тело = json.dumps({"эмоция": "спокоен", "говорит": "К сожалению"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            url + "/avatar/state", data=тело, method="POST"), timeout=5).read()
        with urllib.request.urlopen(url + "/avatar/state", timeout=5) as r:
            check("ответ модели через мост — персонаж огорчён",
                  json.loads(r.read())["эмоция"], "огорчён")
    finally:
        srv.shutdown()


def test_scenes() -> None:
    """Сценки: персонаж живёт сам — по поводу и без, не повторяясь.

    Движок чистый (face/scenes.py): всё время снаружи, жребий свой. Здесь
    проверяется не «красиво ли», а что события ловятся ровно раз, перебивают
    фон, фон идёт с паузами и без повторов подряд, речь не перебивается,
    ночью засыпает, — и что библиотека цела: сто с лишним сценок, все поводы
    покрыты, числа в разумных пределах.
    """
    section("сценки: жизнь в покое")
    import random
    import sys as _sys
    import time
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    # --- библиотека --------------------------------------------------------
    check("сценок — сто с лишним", len(sc.СЦЕНКИ) >= 100, True)
    check("имена не повторяются", len(sc.ПО_ИМЕНИ), len(sc.СЦЕНКИ))
    check("у каждой сценки известный повод (или это звено цепочки)",
          [с.имя for с in sc.СЦЕНКИ if с.когда not in sc.СОБЫТИЯ + sc.ФОНЫ + (sc.ЦЕПОЧКА,)], [])
    check("у каждого звена цепочки есть тот, кто его зовёт «следом»",
          [с.имя for с in sc.СЦЕНКИ if с.когда == sc.ЦЕПОЧКА
           and not any(д.следом == с.имя for д in sc.СЦЕНКИ)], [])
    check("«следом» всегда указывает на существующую сценку",
          [с.имя for с in sc.СЦЕНКИ if с.следом and с.следом not in sc.ПО_ИМЕНИ], [])
    check("руки в сценках — наружу, 0..60 градусов",
          [с.имя for с in sc.СЦЕНКИ for _, п in с.кадры
           for к in (sc.РУКА_Л, sc.РУКА_П) if к in п and not 0 <= п[к] <= 60], [])
    check("жесты руками есть: привет, пока, не понял, рад, думает",
          [any(sc.РУКА_П in п or sc.РУКА_Л in п for с in sc.СЦЕНКИ if с.когда == повод for _, п in с.кадры)
           for повод in ("человек_пришёл", "услышал_привет", "услышал_пока", "не_понял", "рад", "думает")],
          [True] * 6)
    check("«машет» — жест назван честно: рука в первом взмахе действительно поднимается",
          sc.ПО_ИМЕНИ["машет"].параметры(0.25).get(sc.РУКА_П, 0.0) > 0, True)
    check("каждый повод хоть чем-то отыгран",
          [п for п in sc.СОБЫТИЯ + sc.ФОНЫ if not any(с.когда == п for с in sc.СЦЕНКИ)], [])
    # ЧЕТЫРЕ НА ПОВОД — НЕ КРУГЛОЕ ЧИСЛО ИЗ ГОЛОВЫ. При одной сценке повод
    # играется ВСЕГДА одинаково: таймер звонит — всегда «дзинь», сказали
    # «спасибо» — всегда тот же поклон. Редкий повод тем и заметен, что его
    # ждёшь, и один-единственный отклик выдаёт механизм вернее, чем сотня
    # одинаковых сценок в покое: там взгляд скользит, здесь — упирается.
    from collections import Counter as _Счёт
    скольких = _Счёт(с.когда for с in sc.СЦЕНКИ)
    check("у каждого повода не меньше четырёх сценок — иначе он играется всегда одинаково",
          sorted(п for п in sc.СОБЫТИЯ + sc.ФОНЫ if скольких[п] < 4), [])
    # ПРИОРИТЕТ ОДИН НА ВЕСЬ ПОВОД. Разнобой внутри пула значит, что «позвали
    # по имени» перебивает идущую сценку или не перебивает — по жребию, какая
    # выпала. Глазами это не видно, потому и сторож.
    приоритеты = {}
    for с in sc.СЦЕНКИ:
        приоритеты.setdefault(с.когда, set()).add(с.приоритет)
    # ЦЕПОЧКИ — ЭТО ЗАМЫСЕЛ БЕЗ МОДЕЛИ, и их должно быть много. Одиночный
    # жест читается как реакция, два-три подряд — как НАМЕРЕНИЕ: «заметил
    # что-то сбоку → пошёл поглядеть → ничего интересного». Ровно за этим и
    # ходят к модели на ПК; механизм же (Сценка.следом) работал с самого
    # начала и был использован ОДИН раз на четыреста с лишним сценок.
    головы = [с for с in sc.СЦЕНКИ if с.следом]
    check("цепочек в библиотеке много, а не одна", len(головы) >= 12, True)
    # Звено обязано продолжать позу предыдущего, иначе между ними рывок и
    # вместо истории выходит склейка. Сверяем последний кадр головы с первым
    # кадром звена по тем ключам, которые в обоих есть.
    рывки = []
    for с in головы:
        конец = с.параметры(1.0)
        начало = sc.ПО_ИМЕНИ[с.следом].параметры(0.0)
        общие = set(конец) & set(начало)
        for ключ in общие:
            предел = 0.02 if ключ in (sc.X, sc.Y) else (
                0.05 if ключ in (sc.МАСШТАБ,) else 3.0)
            if abs(конец[ключ] - начало[ключ]) > предел:
                рывки.append(f"{с.имя}→{с.следом}: {ключ}")
    check("между звеньями цепочки нет рывка: звено начинается там, где кончилось прошлое",
          рывки, [])
    check("звенья цепочки не ждут отдыха — история не должна прерываться",
          [с.имя for с in sc.СЦЕНКИ if с.когда == sc.ЦЕПОЧКА and с.отдых], [])
    check("у всех сценок одного повода один приоритет",
          sorted(п for п, зн in приоритеты.items() if len(зн) > 1), [])
    # ФОНЫ, КОТОРЫЕ ДЕРЖАТСЯ ДОЛГО, — ОСОБЫЙ СЛУЧАЙ. «Говорит», «танец» и
    # «ночь спит» идут кругами без пауз, пока держится повод; пул на четыре
    # сценки проходит круг за восемь секунд, и дальше человек видит одно и то
    # же. Хуже всего это у речи: ровно тогда на персонажа смотрят в упор.
    check("у долгих фонов пул шире четырёх: речь и танец идут кругами",
          {п: скольких[п] >= 8 for п in ("говорит", "танец")},
          {"говорит": True, "танец": True})
    странные = []
    for с in sc.СЦЕНКИ:
        if с.длительность <= 0:
            странные.append(f"{с.имя}: длительность")
        for д in (0.0, 0.3, 0.5, 0.7, 1.0):
            п = с.параметры(д)
            if not 0.5 <= п.get(sc.МАСШТАБ, 1.0) <= 1.5:
                странные.append(f"{с.имя}: масштаб {п[sc.МАСШТАБ]}")
            if abs(п.get(sc.X, 0.0)) > 0.5 or abs(п.get(sc.Y, 0.0)) > 0.2:
                странные.append(f"{с.имя}: сдвиг за экран")
            for к in (sc.ГОЛОВА_X, sc.ГОЛОВА_Y, sc.ГОЛОВА_Z):
                if abs(п.get(к, 0.0)) > 30:
                    странные.append(f"{с.имя}: голова {к}")
    check("числа в пределах: масштаб 0.5–1.5, сдвиг в экране, голова до 30°", странные, [])
    зевок = sc.ПО_ИМЕНИ["зевнул"]
    check("между кадрами — плавно, а не рывком: на полпути к раскрытому рту он полуоткрыт",
          0.3 < зевок.параметры(0.2)[sc.РОТ] < 0.9, True)
    check("вне кадров, где ключа нет, — покой (ноль), масштаб — единица",
          (sc.ПО_ИМЕНИ["потянулся"].параметры(0.0)[sc.МАСШТАБ], зевок.параметры(1.0)[sc.РОТ]),
          (1.0, 0.0))

    # --- события ---------------------------------------------------------------
    сц = sc.Сценарист(random.Random(1))
    покой = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    сц.кадр(покой, 0.0)
    пришёл = сц.кадр(dict(покой, человек=True, взгляд=0.0), 0.1)
    check("человек появился — событийная сценка сразу",
          sc.ПО_ИМЕНИ[пришёл["имя"]].когда if пришёл["имя"] else "", "человек_пришёл")
    ещё = сц.кадр(dict(покой, человек=True, взгляд=0.0), 0.2)
    check("…та же сценка продолжается, а не начинается заново каждый кадр",
          ещё["имя"], пришёл["имя"])
    check("сдвиги есть, и они числа",
          all(isinstance(з, float) for з in ещё["параметры"].values()) and bool(ещё["параметры"]), True)
    # Движение: пеленг скакнул.
    сц2 = sc.Сценарист(random.Random(2))
    сц2.кадр(dict(покой, человек=True, взгляд=0.0), 0.0)
    for i in range(1, 60):
        сц2.кадр(dict(покой, человек=True, взгляд=0.0), i / 10)
    д = сц2.кадр(dict(покой, человек=True, взгляд=0.6), 6.0)
    check("пеленг скакнул — «движение»",
          sc.ПО_ИМЕНИ[д["имя"]].когда if д["имя"] else "", "движение")
    # Слова.
    сц3 = sc.Сценарист(random.Random(3))
    сц3.кадр(покой, 0.0)
    с3 = сц3.кадр(dict(покой, услышал="Кузя, спасибо тебе"), 0.1)
    check("услышал «спасибо» — сценка про спасибо (их несколько, любая годится)",
          sc.ПО_ИМЕНИ[с3["имя"]].когда if с3["имя"] else "", "услышал_спасибо")
    с3 = сц3.кадр(dict(покой, услышал="Кузя, спасибо тебе"), 5.0)
    check("то же услышанное второй раз не повторяет сценку", с3["имя"] != "поклон", True)
    # Музыка: событие, потом цикл танца, пока играет; кончилась — поклон.
    сц4 = sc.Сценарист(random.Random(4))
    сц4.кадр(покой, 0.0)
    м = dict(покой, музыка={"играет": True, "название": "x"})
    н = сц4.кадр(м, 0.1)
    check("музыка началась — сценка на этот повод («услышал музыку» или «выкатил музыку»)",
          sc.ПО_ИМЕНИ[н["имя"]].когда if н["имя"] else "", "музыка_началась")
    имена = set()
    for i in range(2, 300):
        р = сц4.кадр(м, i / 10)
        имена.add(sc.ПО_ИМЕНИ[р["имя"]].когда if р["имя"] else "")
    check("пока играет — только танцы (и они идут кругами, без пауз)",
          имена <= {"танец", "музыка_началась"} and "танец" in имена, True)
    к = сц4.кадр(покой, 30.1)
    check("музыка кончилась — сценка на конец музыки, танец брошен",
          sc.ПО_ИМЕНИ[к["имя"]].когда if к["имя"] else "", "музыка_кончилась")
    # Речь: только жесты «говорит», ничего другого.
    сц5 = sc.Сценарист(random.Random(5))
    г = dict(покой, говорит="Привет, я Кузя")
    поводы = set()
    for i in range(0, 400):
        р = сц5.кадр(г, i / 10)
        if р["имя"]:
            поводы.add(sc.ПО_ИМЕНИ[р["имя"]].когда)
    check("пока говорит — только жесты речи", поводы, {"говорит"})
    # Сплю — ничего.
    сц6 = sc.Сценарист(random.Random(6))
    check("голос молчит («сплю») — сценок нет",
          any(сц6.кадр({"эмоция": "сплю"}, i / 10)["имя"] for i in range(0, 600)), False)
    # Тревога перебивает фоновую сценку немедленно.
    сц7 = sc.Сценарист(random.Random(7))
    т = 0.0
    while not сц7.кадр(покой, т)["имя"]:
        т += 0.1
    фоновая = сц7.кадр(покой, т)["имя"]
    тревога = сц7.кадр(dict(покой, эмоция="встревожен"), т + 0.1)
    check("тревога перебивает фоновую сценку немедленно",
          (фоновая != тревога["имя"],
           sc.ПО_ИМЕНИ[тревога["имя"]].когда if тревога["имя"] else ""),
          (True, "тревога"))

    # --- фон: паузы, повторы, ночь ------------------------------------------
    сц8 = sc.Сценарист(random.Random(8))
    имена = []
    паузы = []
    прошлое_имя, кончилась_в = "", None
    for i in range(0, 6000):
        р = сц8.кадр(покой, i / 10, час=14, минута=5)
        if р["имя"] and р["имя"] != прошлое_имя:
            if кончилась_в is not None:
                паузы.append(i / 10 - кончилась_в)
            имена.append(р["имя"])
        if not р["имя"] and прошлое_имя:
            кончилась_в = i / 10
        прошлое_имя = р["имя"]
    check("за десять минут покоя — десятки сценок, и они разные",
          (len(имена) >= 40, len(set(имена)) >= 25), (True, True))
    check("одна и та же сценка не идёт два раза подряд",
          any(a == b for a, b in zip(имена, имена[1:])), False)
    check("между сценками — паузы, не короче трёх секунд",
          min(паузы) >= sc.ПАУЗА_ПОКОЯ[0] - 1e-6, True)
    check("после трёх минут одиночества — сценки «долго один»",
          any(sc.ПО_ИМЕНИ[и].когда == "долго_один" for и in имена), True)
    # Ночь: сначала клюёт носом, через две минуты спит.
    сц9 = sc.Сценарист(random.Random(9))
    ночь = dict(покой, ночь=True)
    поводы_ночи = set()
    for i in range(0, 3000):
        р = сц9.кадр(ночь, i / 10, час=1, минута=15)
        if р["имя"]:
            поводы_ночи.add(sc.ПО_ИМЕНИ[р["имя"]].когда)
    check("ночью — ночные сценки и сон, дневных нет",
          поводы_ночи <= {"покой_ночью", "ночь_спит"} and "ночь_спит" in поводы_ночи, True)
    # НОЧЬЮ ПРИ ЧЕЛОВЕКЕ — ТОЖЕ НОЧНЫЕ. Пустая комната ночью проверялась
    # давно, а вот подойти к роботу в полтретьего забыли: там ветка другая, и
    # она годами отдавала дневной пул «человек_рядом». Персонаж бодро
    # потягивался, прищуривался вдаль и подмигивал — в три часа ночи.
    # Множителем это не лечится: дневных сценок под сотню, ночных шестнадцать,
    # большой пул обходит малый любым перевесом, потому что у него всегда есть
    # свежая. Лечится только тем, что ночью дневного пула нет.
    сц9н = sc.Сценарист(random.Random(93))
    ночь_с_человеком = dict(покой, ночь=True, человек=True, взгляд=0.0)
    поводы_н, имена_н = set(), set()
    for i in range(0, 6000):
        р = сц9н.кадр(ночь_с_человеком, i / 10, час=2, минута=40)
        if р["имя"]:
            поводы_н.add(sc.ПО_ИМЕНИ[р["имя"]].когда)
            имена_н.add(р["имя"])
    check("ночью при человеке дневных сценок нет — только ночные и события",
          "человек_рядом" not in поводы_н and "покой" not in поводы_н, True)
    # И не выродиться в две сценки по кругу: ночной пул должен держать ночь
    # сам, иначе запрет дневных превращает существо в спящую картинку.
    check("ночной пул тянет ночь сам: за десять минут при человеке — больше пяти разных",
          len(имена_н) > 5, True)
    # Спал — и кто-то подошёл: это не бодрое «человек пришёл» (тот машет и
    # подпрыгивает), а «проснулся» — сначала открыть глаза. Экран в ту же
    # секунду разгорается до дневного (face.py, ПРОСНУЛСЯ_ДЕРЖИМ).
    сц9б = sc.Сценарист(random.Random(91))
    for i in range(0, 3000):                       # доводим до сна
        сц9б.кадр(ночь, i / 10, час=1, минута=15)
    спал = сц9б._спал
    проснулся = ""
    for i in range(3000, 3040):
        р = сц9б.кадр(dict(ночь, человек=True, взгляд=0.0), i / 10, час=1, минута=15)
        if р["имя"] and sc.ПО_ИМЕНИ[р["имя"]].когда == "проснулся":
            проснулся = р["имя"]
            break
    check("спал ночью, человек подошёл — сначала просыпается, а не машет с ходу",
          (спал, bool(проснулся)), (True, True))
    # А днём (не спал) — обычная встреча, «проснулся» тут не при чём.
    сц9в = sc.Сценарист(random.Random(92))
    сц9в.кадр(покой, 0.0, час=13, минута=0)
    поводы_днём = set()
    for i in range(1, 40):
        р = сц9в.кадр(dict(покой, человек=True, взгляд=0.0), i / 10, час=13, минута=0)
        if р["имя"]:
            поводы_днём.add(sc.ПО_ИМЕНИ[р["имя"]].когда)
    check("днём, не поспав, он не «просыпается» — просто встречает",
          "проснулся" in поводы_днём, False)
    # Утро — только утром.
    сц10 = sc.Сценарист(random.Random(10))
    утро = set()
    for i in range(0, 3000):
        р = сц10.кадр(покой, i / 10, час=8, минута=15)
        if р["имя"]:
            утро.add(sc.ПО_ИМЕНИ[р["имя"]].когда)
    check("утром бывают утренние сценки", "покой_утром" in утро, True)
    # Час пробил — ровно раз.
    сц11 = sc.Сценарист(random.Random(12))
    сц11.кадр(покой, 0.0, час=13, минута=59)
    пробило = [сц11.кадр(покой, 1.0 + i / 10, час=14, минута=0)["имя"] for i in range(0, 5)]
    check("пробило час — сценка часов", sc.ПО_ИМЕНИ[пробило[0]].когда if пробило[0] else "", "час_пробил")
    # Таймер: скоро и кончился.
    сц12 = sc.Сценарист(random.Random(13))
    сц12.кадр(dict(покой, таймеры=[{"имя": "чай", "осталось": 30}]), 0.0)
    с12 = сц12.кадр(dict(покой, таймеры=[{"имя": "чай", "осталось": 9}]), 0.1)
    check("до конца таймера десять секунд — сценка «таймер скоро»",
          sc.ПО_ИМЕНИ[с12["имя"]].когда if с12["имя"] else "", "таймер_скоро")
    for i in range(2, 40):
        сц12.кадр(dict(покой, таймеры=[{"имя": "чай", "осталось": 1}]), i / 10)
    с12 = сц12.кадр(dict(покой, таймеры=[]), 4.1)
    check("таймер кончился — сценка на звонок",
          sc.ПО_ИМЕНИ[с12["имя"]].когда if с12["имя"] else "", "таймер_кончился")

    # --- пропал человек: цепочка поиска в ту сторону, где он был ------------
    # Звенья идут друг за другом без пауз и жребия; сторона — по последнему
    # пеленгу; вернулся посреди поиска — «нашёлся», а не «пришёл».
    def прогнать_поиск(семя: int, пеленг: float) -> list[tuple[str, float]]:
        сцн = sc.Сценарист(random.Random(семя))
        рядом = dict(покой, человек=True, взгляд=пеленг)
        for i in range(0, 20):
            сцн.кадр(рядом, i / 10)
        сцн.кадр(dict(покой, человек=False), 2.0)
        сцн._начать(sc.ПО_ИМЕНИ["пошёл искать"], 2.0)     # жребий — не предмет проверки
        ход = []
        t = 2.0
        while t < 20.0:
            р = сцн.кадр(dict(покой, человек=False), t)
            if not ход or ход[-1][0] != р["имя"]:
                ход.append((р["имя"], р.get("x", 0.0)))
            t += 0.1
        return ход
    ход = прогнать_поиск(21, -0.5)                         # человек был СПРАВА
    check("поиск — цепочка из шести звеньев подряд, без пауз",
          [и for и, _ in ход][:6],
          ["пошёл искать", "выглянул за край", "к другому краю", "никого",
           "пожал плечами", "вернулся на место"])
    def x_звена(ход, имя):
        # Нет такого звена (цепочка оборвалась) — 0.0: проверка падает, а не прогон.
        return next((x for и, x in ход if и == имя), 0.0)
    check("человек был справа — сначала идёт к правому краю (x > 0)",
          x_звена(ход, "выглянул за край") > 0.2, True)
    ход_л = прогнать_поиск(22, +0.5)                       # человек был СЛЕВА
    check("человек был слева — зеркально: сначала к левому краю (x < 0)",
          x_звена(ход_л, "выглянул за край") < -0.2, True)
    check("после цепочки — свободен (пустая сценка или обычный фон), не зациклился",
          any(и == "" or sc.ПО_ИМЕНИ[и].когда in sc.ФОНЫ for и, _ in ход[6:]), True)
    сцн = sc.Сценарист(random.Random(23))
    сцн.кадр(dict(покой, человек=True, взгляд=0.0), 0.0)
    сцн.кадр(dict(покой, человек=False), 0.1)
    сцн._начать(sc.ПО_ИМЕНИ["пошёл искать"], 0.1)
    for i in range(2, 30):
        сцн.кадр(dict(покой, человек=False), i / 10)
    н = сцн.кадр(dict(покой, человек=True, взгляд=0.0), 3.0)
    check("вернулся посреди поиска — «нашёлся», поиск брошен",
          sc.ПО_ИМЕНИ[н["имя"]].когда if н["имя"] else "", "нашёлся")
    сцн24 = sc.Сценарист(random.Random(24))
    check("звенья цепочки жребием по поводу не выбираются — только «следом»",
          [с.имя for повод in sc.СОБЫТИЯ + sc.ФОНЫ for _ in range(20)
           for с in [сцн24._выбрать([повод], 0.0)] if с is not None and с.когда == sc.ЦЕПОЧКА], [])
    check("звено цепочки — не событие и не фон",
          (sc.ЦЕПОЧКА in sc.СОБЫТИЯ, sc.ЦЕПОЧКА in sc.ФОНЫ), (False, False))
    # --- расписание: время суток, дни недели, праздники ------------------------
    # Раз в день каждое, только при человеке; праздник — с текстом даты;
    # свои праздники — через Сценарист(праздники=…).
    def повод(р):
        return sc.ПО_ИМЕНИ[р["имя"]].когда if р["имя"] else ""
    рядом = dict(покой, человек=True, взгляд=0.0)
    сцр = sc.Сценарист(random.Random(31))
    сцр.кадр(покой, 0.0, час=8, минута=0, день=9, месяц=9, день_недели=2)
    у = сцр.кадр(рядом, 0.1, час=8, минута=0, день=9, месяц=9, день_недели=2)
    check("утром вошёл человек — «утро_встреча» раньше безымянного «привет»",
          (повод(у), у["когда"]), ("утро_встреча", "утро_встреча"))
    for i in range(2, 60):
        сцр.кадр(рядом, i / 10, час=8, минута=0, день=9, месяц=9, день_недели=2)
    сцр.кадр(покой, 6.0, час=9, минута=0, день=9, месяц=9, день_недели=2)
    у2 = сцр.кадр(рядом, 6.1, час=9, минута=0, день=9, месяц=9, день_недели=2)
    check("тот же день, вошёл снова — «с добрым утром» второй раз не говорит "
          "(обычная встреча или «нашёлся», если пошёл искать)",
          повод(у2) in ("человек_пришёл", "нашёлся"), True)
    сцр.кадр(покой, 7.0, час=8, минута=0, день=10, месяц=9, день_недели=3)
    у3 = сцр.кадр(рядом, 7.1, час=8, минута=0, день=10, месяц=9, день_недели=3)
    check("назавтра утром — снова здоровается", повод(у3), "утро_встреча")
    сцп = sc.Сценарист(random.Random(32))
    for i in range(0, 30):
        р = сцп.кадр(покой, i / 10, час=8, минута=0, день=9, месяц=9, день_недели=2)
    check("никого нет — расписание молчит (это слова кому-то)",
          повод(р) in ("", "покой", "покой_утром", "долго_один"), True)
    def первое(час, день, месяц, дн, семя=33, **доп):
        сц = sc.Сценарист(random.Random(семя), **доп)
        сц.кадр(покой, 0.0, час=час, минута=0, день=день, месяц=месяц, день_недели=дн)
        return сц.кадр(рядом, 0.1, час=час, минута=0, день=день, месяц=месяц, день_недели=дн)
    check("13:00 — «обед?»; 19:00 — «как прошёл день?»; 23:30 — «не пора спать?»",
          [повод(первое(13, 9, 9, 2)), повод(первое(19, 9, 9, 2)), повод(первое(23, 9, 9, 2))],
          ["обед", "вечер_встреча", "поздно"])
    check("пятница вечером — «пятница!», понедельник утром — «понедельник…» (утро уступает)",
          [повод(первое(18, 12, 9, 4)), [повод(первое(8, 8, 9, 0)), повод(первое(8, 8, 9, 0, семя=34))]],
          ["вечер_встреча", ["утро_встреча", "утро_встреча"]])
    сцпн = sc.Сценарист(random.Random(35))
    сцпн.кадр(покой, 0.0, час=8, минута=0, день=8, месяц=9, день_недели=0)
    поводы_пн = set()
    for i in range(1, 120):
        р = сцпн.кадр(рядом, i / 10, час=8, минута=0, день=8, месяц=9, день_недели=0)
        if р["имя"]:
            поводы_пн.add(повод(р))
    check("в понедельник утром за минуту сказано и «утро», и «понедельник» — оба раз в день",
          {"утро_встреча", "понедельник"} <= поводы_пн, True)
    сцпт = sc.Сценарист(random.Random(36))
    поводы_пт = {повод(р) for р in [сцпт.кадр(рядом, i / 10, час=18, минута=0, день=12, месяц=9, день_недели=4)
                                     for i in range(0, 120)] if р["имя"]}
    check("пятница вечером — «пятница!» тоже отыгрывается (после вечернего приветствия)",
          {"вечер_встреча", "пятница"} <= поводы_пт, True)
    нг = первое(10, 1, 1, 3)
    check("1 января — праздник, и в пузыре именно «С Новым годом!»",
          (повод(нг), нг["текст"]), ("праздник", "С Новым годом!"))
    др = первое(10, 5, 12, 1, семя=37, праздники={"05-12": "С днём рождения, Игорь!"})
    check("свой праздник из config.local.json — со своим текстом",
          (повод(др), др["текст"]), ("праздник", "С днём рождения, Игорь!"))
    check("кривой ключ своего праздника не роняет сценарий",
          sc.Сценарист(праздники={"вчера": "x", "05-12": "ок"}).праздники.get((5, 12)), "ок")
    check("без даты (домовёнок без часов) расписание молчит",
          повод(sc.Сценарист(random.Random(38)).кадр(рядом, 0.1, час=8)) in ("", "человек_пришёл", "покой", "человек_рядом"), True)
    check("в кадре сценки есть повод («когда») — странице для готовых движений модели",
          у3["когда"], "утро_встреча")
    # Тот же путь, каким расписание доезжает до Live2D на ПК: Аватар.состояние()
    # зовёт self.сценарист.кадр() с датой от _часы_дня — не отдельный сценарист,
    # собранный руками в тесте, а настоящий код kuzya_pc.py.
    ав_расп = Аватар(часы=lambda: 100.0)
    ав_расп._часы_дня = lambda: time.struct_time((2026, 9, 7, 8, 0, 0, 0, 1, 0))  # понедельник, утро
    ав_расп.сценарист = sc.Сценарист(random.Random(39))
    ав_расп.принять({"эмоция": "спокоен", "человек": False})
    ав_расп.принять({"эмоция": "спокоен", "человек": True, "взгляд": 0.0})
    check("аватар на ПК: расписание доезжает до сценки («утро_встреча» через настоящий Аватар.состояние())",
          повод(ав_расп.состояние()["сцена"]), "утро_встреча")

    # Страница: руки сценки идут в параметр руки из config.json.
    страница = (_Path(__file__).resolve().parent / "avatar" / "index.html").read_text(encoding="utf-8")
    check("страница переводит руки сценки в параметр руки из config.json",
          ('id === "рука_л" || id === "рука_п"' in страница, "п.рука_размах" in страница), (True, True))
    check("страница подбирает готовые движения модели по поводу сценки и выражения по эмоции",
          ("ДВИЖЕНИЯ_ПО_ПОВОДУ[сцена.когда]" in страница, "motionManager" in страница,
           "ВЫРАЖЕНИЯ_ПО_ЭМОЦИИ[с.эмоция]" in страница), (True, True, True))
    # Свои праздники читаются из config.local.json — и только оттуда.
    import tempfile as _tf
    from kuzya_pc import свои_праздники
    папка = _Path(_tf.mkdtemp())
    check("нет config.local.json — праздников нет, и это не ошибка", свои_праздники(папка), {})
    (папка / "config.local.json").write_text('{"праздники": {"05-12": "С днём рождения!"}}', encoding="utf-8")
    check("праздники из config.local.json", свои_праздники(папка), {"05-12": "С днём рождения!"})
    (папка / "config.local.json").write_text('{"праздники": "ерунда"', encoding="utf-8")
    check("битый файл — пусто, а не падение", свои_праздники(папка), {})
    # Дела: мозг позвал инструмент — персонаж делает это сам, в тот же кадр,
    # каждый вызов заново (по номеру, а не по имени), и любой инструмент
    # даёт хоть какой-то жест.
    check("инструмент → дело: drive едет, play_music музыка, set_timer таймер, "
          "look_around смотрит, remember_person записывает, calculate считает",
          [sc.дело(и) for и in ("drive", "play_music", "set_timer", "look_around",
                                 "remember_person", "calculate")],
          ["делает_едет", "делает_музыку", "делает_таймер", "делает_смотрит",
           "делает_записывает", "делает_считает"])
    check("незнакомый инструмент — общий жест, а не тишина; notes_* — по куску имени",
          (sc.дело("some_new_tool"), sc.дело("notes_add_line"), sc.дело("")),
          ("делает_что_то", "делает_записывает", "делает_что_то"))
    сц13 = sc.Сценарист(random.Random(14))
    думает = dict(покой, эмоция="думаю")
    сц13.кадр(dict(думает, делает={"номер": 0, "что": ""}), 0.0)
    д1 = сц13.кадр(dict(думает, делает={"номер": 1, "что": "drive"}), 0.1)
    check("мозг позвал drive — сценка «едет» в тот же кадр, хоть эмоция «думаю»",
          sc.ПО_ИМЕНИ[д1["имя"]].когда if д1["имя"] else "", "делает_едет")
    д1б = сц13.кадр(dict(думает, делает={"номер": 1, "что": "drive"}), 0.5)
    check("…и тот же номер второй раз сценку не перезапускает", д1б["имя"], д1["имя"])
    for i in range(6, 40):
        сц13.кадр(dict(думает, делает={"номер": 1, "что": "drive"}), i / 10)
    д2 = сц13.кадр(dict(думает, делает={"номер": 2, "что": "drive"}), 4.1)
    check("второй drive (новый номер) — снова жест, отдых у дел нулевой",
          sc.ПО_ИМЕНИ[д2["имя"]].когда if д2["имя"] else "", "делает_едет")
    д3 = сц13.кадр(dict(думает, делает={"номер": 3, "что": "set_timer"}), 4.2)
    check("следующий инструмент перебивает жест предыдущего сразу",
          sc.ПО_ИМЕНИ[д3["имя"]].когда if д3["имя"] else "", "делает_таймер")
    check("у дел есть слово в пузырь и они короткие — не переживут ответ",
          all(с.длительность <= 2.5 for с in sc.СЦЕНКИ if с.когда.startswith("делает_"))
          and any(с.текст for с in sc.СЦЕНКИ if с.когда == "делает_едет"), True)

    # --- личное дело узнало день рождения — персонаж празднует с именем -----
    # Тот же приём, что у «делает»: номер, а не текст, — повтор кадра с тем
    # же текстом не перезапускает сценку. Голосовая служба берёт текст из
    # people.отпраздновать() (см. voice/robot_voice/app.py, voice/selftest.py
    # test_день_рождения) — здесь проверяется только сам сценарист.
    сц14 = sc.Сценарист(random.Random(40))
    сц14.кадр(dict(думает, именины={"номер": 0, "текст": ""}), 0.0)
    им1 = сц14.кадр(dict(думает, именины={"номер": 1, "текст": "С днём рождения, Игорь!"}), 0.1)
    check("новый номер именин — сценка «праздник», сразу, хоть эмоция «думаю»",
          (sc.ПО_ИМЕНИ[им1["имя"]].когда if им1["имя"] else "", им1["текст"]),
          ("праздник", "С днём рождения, Игорь!"))
    им1б = сц14.кадр(dict(думает, именины={"номер": 1, "текст": "С днём рождения, Игорь!"}), 0.5)
    check("тот же номер второй раз сценку не перезапускает", им1б["имя"], им1["имя"])
    сц15 = sc.Сценарист(random.Random(42))
    без = сц15.кадр(dict(покой, именины={"номер": 0, "текст": ""}), 0.0)
    check("именины «номер 0» (никого не поздравляли) — не «праздник»",
          bool(без["имя"]) and sc.ПО_ИМЕНИ[без["имя"]].когда == "праздник", False)

    # --- человек в кадре молчит: сперва «рядом», потом зовёт внимание --------
    # Детям интереснее всего это: по стеклу, носом к стеклу, прячется и
    # выглядывает, рожицы. Только при человеке, только после ВНИМАНИЕ_ЧЕРЕЗ
    # секунд молчания, с короткими паузами; разговор снимает зов.
    check("сценки зова внимания есть, и их много: по стеклу, машет, прячется, рожицы",
          {"стучит по стеклу", "машет обеими: эй", "прячется и выглядывает", "корчит рожицу",
           "прижался носом к стеклу"} <= {с.имя for с in sc.СЦЕНКИ if с.когда == "зовёт_внимание"}
          and sum(1 for с in sc.СЦЕНКИ if с.когда == "зовёт_внимание") >= 12, True)
    check("зов — фон, не событие: перебивается любым событием", "зовёт_внимание" in sc.ФОНЫ, True)

    def поводы_за(сценарист, состояние, от, до, шаг=0.1, **доп):
        имена = []
        t = от
        while t < до:
            р = сценарист.кадр(состояние, round(t, 3), **доп)
            if р["имя"] and (not имена or имена[-1] != р["имя"]):
                имена.append(р["имя"])
            t += шаг
        return {sc.ПО_ИМЕНИ[и].когда for и in имена}, имена

    сцв = sc.Сценарист(random.Random(51))
    сцв.кадр(покой, 0.0)
    рано, _ = поводы_за(сцв, рядом, 0.1, sc.ВНИМАНИЕ_ЧЕРЕЗ - 1.0)
    check("первые секунды при человеке — встреча и «рядом», зова ещё нет",
          ("зовёт_внимание" in рано, рано <= {"человек_пришёл", "человек_рядом", "покой"}), (False, True))
    поздно, имена_зова = поводы_за(сцв, рядом, sc.ВНИМАНИЕ_ЧЕРЕЗ - 1.0, sc.ВНИМАНИЕ_ЧЕРЕЗ + 40.0)
    check("молчит дольше ВНИМАНИЕ_ЧЕРЕЗ — зовёт внимание, и не одним трюком",
          ("зовёт_внимание" in поздно,
           len({и for и in имена_зова if sc.ПО_ИМЕНИ[и].когда == "зовёт_внимание"}) >= 5),
          (True, True))
    check("сцен при человеке за 40 секунд — не меньше десяти: паузы короткие, не 3–9 с",
          len(имена_зова) >= 10, True)
    # Позвали по имени посреди зова — счёт молчания начинается заново.
    сцв.кадр(dict(рядом, эмоция="слушаю"), sc.ВНИМАНИЕ_ЧЕРЕЗ + 40.1)
    for i in range(1, 30):
        сцв.кадр(dict(рядом, говорит="Привет, я Кузя"), sc.ВНИМАНИЕ_ЧЕРЕЗ + 40.1 + i / 10)
    после, _ = поводы_за(сцв, рядом, sc.ВНИМАНИЕ_ЧЕРЕЗ + 43.2, sc.ВНИМАНИЕ_ЧЕРЕЗ + 43.2 + sc.ВНИМАНИЕ_ЧЕРЕЗ - 1.0)
    check("после разговора зов не продолжается сразу — снова ждёт ВНИМАНИЕ_ЧЕРЕЗ",
          "зовёт_внимание" in после, False)
    check("молчит_рядом: считается с появления человека, 0 — без человека",
          (round(sc.Сценарист(random.Random(52)).молчит_рядом(5.0), 3),
           round(сцв.молчит_рядом(sc.ВНИМАНИЕ_ЧЕРЕЗ + 43.2 + 2.0), 1) > 0.0), (0.0, True))
    сцв2 = sc.Сценарист(random.Random(53))
    сцв2.кадр(покой, 0.0)
    один, _ = поводы_за(сцв2, покой, 0.1, 60.0)
    check("никого нет — зова нет: звать некого", "зовёт_внимание" in один, False)

    # --- карточки его руками: достал, убрал, реакция на погоду не пропала ----
    сцк = sc.Сценарист(random.Random(61))
    дождь = dict(покой, погода={"t": 12, "код": 61, "описание": "дождь"})
    сцк.кадр(покой, 0.0)
    к1 = сцк.кадр(дождь, 0.1)
    check("появилась карточка погоды — сначала «достал карточку», не дождь",
          sc.ПО_ИМЕНИ[к1["имя"]].когда if к1["имя"] else "", "карточка_появилась")
    поводы_к, имена_к = поводы_за(сцк, дождь, 0.2, 6.0)
    check("…а дождь не потерян: отыгран следом, после жеста",
          ("дождь" in поводы_к, имена_к[0] == к1["имя"]), (True, True))
    сцк.кадр(дождь, 39.0)
    у = сцк.кадр(покой, 39.1)
    check("карточка ушла (сорок секунд вышли) — «убрал карточку»",
          sc.ПО_ИМЕНИ[у["имя"]].когда if у["имя"] else "", "карточка_убрана")
    сцк0 = sc.Сценарист(random.Random(62))
    п0 = сцк0.кадр(дождь, 0.0)
    check("первый кадр уже с карточкой — не «достаёт» (нечего), а на дождь реагирует",
          sc.ПО_ИМЕНИ[п0["имя"]].когда if п0["имя"] else "", "дождь")
    сцк2 = sc.Сценарист(random.Random(63))
    сцк2.кадр(покой, 0.0)
    сцк2._начать(sc.ПО_ИМЕНИ["достал карточку"], 0.0)
    for i in range(1, int(sc.ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ * 10) + 30):
        сцк2._начать(sc.ПО_ИМЕНИ["достал карточку"], i / 10)   # жест всё тянется
        сцк2.кадр(дождь if i == 1 else дождь, i / 10)
    сцк2._сцена = None
    устарело, _ = поводы_за(сцк2, дождь, sc.ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ + 3.1, sc.ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ + 10.0)
    check("отложенная реакция старше ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ — забыта, не запоздалый «брр»",
          "дождь" in устарело, False)

    # --- «услышал привет» во время сценки «имя» — раньше терялось совсем ------
    # Живая гонка: «Кузя, привет!» ставит эмоцию «слушаю» (сценка «имя»,
    # приоритет=3, 1.5–2 с) РАНЬШЕ, чем распознавание успевает отдать текст;
    # «услышал_привет» приходит, пока «имя» ещё идёт, приоритета перебить её
    # не хватает — и раньше просто пропадало, ни тогда, ни потом (не было в
    # ОТКЛАДЫВАЕМЫЕ). Человечек кивал на имя и на этом всё, «привет» рукой
    # не отвечал никогда.
    # Пул «имя» — три сценки вперемешку, и не у всех приоритет=3 (у
    # «отдал честь» — 0, он и сам уступил бы). Гонка живая ровно когда
    # выпадет «навострил уши»/«наклонился к собеседнику» — берём такую
    # напрямую (_начать), а не жребием, чтобы тест не зависел от удачи.
    сцпр = sc.Сценарист(random.Random(72))
    сцпр.кадр(покой, 0.0)
    сцпр.кадр(dict(покой, человек=True, эмоция="слушаю"), 0.05)
    сцпр._начать(sc.ПО_ИМЕНИ["навострил уши"], 0.05)
    check("сценка «имя» с приоритетом 3 ещё идёт, когда приходит «услышал»",
          sc.ПО_ИМЕНИ["навострил уши"].приоритет >= 3, True)
    привет_пока_слушает = dict(покой, человек=True, эмоция="слушаю", услышал="Кузя, привет")
    поводы_пр, _ = поводы_за(сцпр, привет_пока_слушает, 0.1, 6.0)
    check("«услышал_привет» пришло, пока «имя» ещё идёт — не потеряно, доиграно следом",
          "услышал_привет" in поводы_пр, True)

    # Музыка: своя пара — «выкатил музыку» / «убрал музыку».
    check("на музыку — карточные жесты того же повода, что и раньше",
          ({с.когда for с in sc.СЦЕНКИ if с.имя in ("выкатил музыку", "убрал музыку")}),
          {"музыка_началась", "музыка_кончилась"})
    # Таймер повис — «повесил», но жест инструмента «заводит таймер» важнее.
    сцт = sc.Сценарист(random.Random(64))
    сцт.кадр(покой, 0.0)
    т1 = сцт.кадр(dict(покой, таймеры=[{"имя": "чай", "осталось": 300}]), 0.1)
    check("новый таймер в строке — сценка «таймер поставлен»",
          sc.ПО_ИМЕНИ[т1["имя"]].когда if т1["имя"] else "", "таймер_поставлен")
    сцт2 = sc.Сценарист(random.Random(65))
    сцт2.кадр(dict(думает, делает={"номер": 0, "что": ""}), 0.0)
    з = сцт2.кадр(dict(думает, делает={"номер": 1, "что": "set_timer"}), 0.1)
    з2 = сцт2.кадр(dict(думает, делает={"номер": 1, "что": "set_timer"},
                        таймеры=[{"имя": "чай", "осталось": 300}]), 0.4)
    check("таймер появился посреди «заводит таймер» — жест инструмента не перебит",
          (sc.ПО_ИМЕНИ[з["имя"]].когда, з2["имя"]), ("делает_таймер", з["имя"]))
    # Подсказка сменилась — показал на неё (редко: долгий отдых) и только
    # тому, кто в кадре: надпись меняется сама каждые 20 секунд, и тыкать в
    # неё пальцем в пустой комнате не для кого.
    рядом = dict(покой, человек=True, взгляд=0.0)
    сцп2 = sc.Сценарист(random.Random(66))
    сцп2.кадр(dict(рядом, подсказка="Скажи: «Кузя, включи радио»"), 0.0)
    # Дать «человек пришёл» отыграть: иначе встреча перебьёт жест на подсказку.
    for i in range(60):
        сцп2.кадр(dict(рядом, подсказка="Скажи: «Кузя, включи радио»"), 0.1 + i * 0.1)
    п1 = сцп2.кадр(dict(рядом, подсказка="Скажи: «Кузя, который час»"), 6.2)
    check("сменилась подсказка «Скажи: …» — жест на неё",
          sc.ПО_ИМЕНИ[п1["имя"]].когда if п1["имя"] else "", "подсказка")
    сцп3 = sc.Сценарист(random.Random(66))
    сцп3.кадр(dict(покой, подсказка="Скажи: «Кузя, включи радио»"), 0.0)
    п2 = сцп3.кадр(dict(покой, подсказка="Скажи: «Кузя, который час»"), 0.1)
    check("подсказка сменилась, а в кадре никого — жеста нет",
          sc.ПО_ИМЕНИ[п2["имя"]].когда if п2["имя"] else "", "покой")
    # Час ночью в пустой комнате: раньше эта же строка каждые 20 секунд
    # сбрасывала счётчик покоя, и робот НИ РАЗУ не засыпал и не скучал.
    сцн = sc.Сценарист(random.Random(9))
    спал = 0
    for i in range(3600 * 5):
        t = i / 5
        сн = dict(покой, ночь=True, подсказка=f"Скажи: вариант {int(t / 20.0)}")
        сцн.кадр(сн, t, час=2)
        if сцн._фоны(сн, t, 2)[:1] == ["ночь_спит"]:
            спал += 1
    check("подсказка не мешает уснуть: за час ночью спит больше сорока минут",
          спал / 5 > 2400, True)
    check("карточные и подсказочные сценки коротки: не переживут саму карточку",
          all(с.длительность <= 2.5 for с in sc.СЦЕНКИ
              if с.когда in ("карточка_появилась", "карточка_убрана", "таймер_поставлен", "подсказка")), True)

    # --- глаза открыты, пока сценка не закрыла их нарочно ----------------------
    # У век ноль — это ЗАКРЫТЫЙ глаз, поэтому покой у них единица, а не ноль,
    # как у сдвигов. Иначе сценке хватало упомянуть веко в одном кадре, чтобы
    # весь остальной хронометраж играть зажмуренной.
    def доля_закрытых(сц):
        закрыто = 0
        for i in range(41):
            п = сц.параметры(i / 40)
            if min(п.get(sc.ВЕКО_Л, 1.0), п.get(sc.ВЕКО_П, 1.0)) < 0.35:
                закрыто += 1
        return закрыто / 41
    подмиг = sc.ПО_ИМЕНИ["подмигнул"]
    check("подмигнул: правый глаз открыт всю сценку, левый закрывается",
          (min(подмиг.параметры(д).get(sc.ВЕКО_П, 1.0) for д in (0.0, 0.25, 0.5, 0.75, 1.0)) > 0.99,
           подмиг.параметры(0.5).get(sc.ВЕКО_Л, 1.0) < 0.05), (True, True))
    check("подмигнул начинается и кончается с открытыми глазами",
          (подмиг.параметры(0.0).get(sc.ВЕКО_Л, 1.0), подмиг.параметры(1.0).get(sc.ВЕКО_Л, 1.0)),
          (1.0, 1.0))
    check("«стучит по стеклу» смотрит на человека, а не жмурится",
          доля_закрытых(sc.ПО_ИМЕНИ["стучит по стеклу"]) < 0.05, True)
    # Ночным сценкам тяжёлые веки положены по смыслу — зевок и медленное
    # моргание иначе не сыграть, — поэтому их считаем отдельно. Но и им не
    # даём играть вслепую: сценка ночного ПОКОЯ, зажмуренная почти целиком,
    # неотличима от сна, а спит персонаж другим пулом.
    ночные = [с for с in sc.СЦЕНКИ if с.когда == "покой_ночью"]
    зажмуренные = sorted(с.имя for с in sc.СЦЕНКИ
                         if с.когда not in ("покой_ночью", "ночь_спит")
                         and доля_закрытых(с) > 0.5)
    check("с закрытыми глазами больше половины времени — только медитация и рожицы",
          зажмуренные,
          ["закрыл лицо ладонями", "корчит рожицу", "медитирует", "подмигнул",
           "показал язык", "потёр глаз", "притворился спящим", "сморщил нос"])
    check("ночная сценка не играет вслепую: покой ночью — это не сон",
          sorted(с.имя for с in ночные if доля_закрытых(с) > 0.75), [])
    # А сон — наоборот: глаза закрыты всю сценку. Спящий с открытыми глазами
    # — не спящий, и весь ночной пул тогда теряет смысл.
    check("сон играется с закрытыми глазами",
          sorted(с.имя for с in sc.СЦЕНКИ
                 if с.когда == "ночь_спит" and доля_закрытых(с) < 0.9), [])
    check("масштаб в кадре без ключа — единица, а не ноль (иначе персонаж схлопнется)",
          sc.ПО_ИМЕНИ["стучит по стеклу"].параметры(0.0).get(sc.МАСШТАБ, 1.0), 1.0)
    # Вход в первый кадр — ИЗ ПОКОЯ, а не из нуля. Все 192 сценки в
    # библиотеке начинаются с кадра на 0.0, поэтому на них эта ветка не
    # видна; проверяем на сценке-пробе, у которой первый кадр стоит позже.
    # Правило нужно любой будущей сценке: у век и масштаба ноль — не покой,
    # а закрытые глаза и схлопнувшийся персонаж.
    поздняя = sc.Сценка("проба-входа", "покой", 1.0,
                        [(0.4, {sc.ВЕКО_Л: 0.0, sc.МАСШТАБ: 1.2, sc.ГОЛОВА_X: 20})])
    четверть = поздняя.параметры(0.2)
    check("до первого кадра сценка едет из покоя: глаз открыт, персонаж не из точки",
          (0.4 < четверть[sc.ВЕКО_Л] < 0.6, 1.05 < четверть[sc.МАСШТАБ] < 1.15,
           5 < четверть[sc.ГОЛОВА_X] < 15), (True, True, True))
    check("…а к самому кадру приходит ровно в заданное",
          (поздняя.параметры(0.4)[sc.ВЕКО_Л], поздняя.параметры(0.4)[sc.МАСШТАБ]),
          (0.0, 1.2))

    # --- зов внимания: с потолком, с отдыхом и не ночью ------------------------
    # Раньше режим включался через 12 секунд и держался, пока человек в кадре:
    # за час выходило 766 сценок «эй!», и ровно столько же в три часа ночи.
    def прогон_зова(ночь, секунд=1800, шаг=0.2):
        сц = sc.Сценарист(random.Random(3))
        зовёт = 0
        сост = {"эмоция": "спокоен", "человек": True, "ночь": ночь, "взгляд": 0.1,
                "музыка": {"играет": False}}
        for i in range(int(секунд / шаг)):
            t = i * шаг
            сц.кадр(сост, t, час=2 if ночь else 14)
            if сц._фоны(сост, t, 2 if ночь else 14)[:1] == ["зовёт_внимание"]:
                зовёт += шаг
        return зовёт
    днём = прогон_зова(False)
    check("зовёт внимание не дольше потолка за раз и с отдыхом: меньше четверти получаса",
          0 < днём < 450, True)
    check("ночью не зовёт вовсе: будить — не его дело", прогон_зова(True), 0)
    check("потолок зова и отдых между зовами объявлены числами",
          (sc.ЗОВЁТ_НЕ_ДОЛЬШЕ > 0, sc.ЗОВЁТ_НЕ_ЧАЩЕ > sc.ЗОВЁТ_НЕ_ДОЛЬШЕ), (True, True))

    # --- «слушаю» и «думаю» больше не выключают жизнь --------------------------
    # Эти две держатся всё окно разговора и всю паузу перед ответом, и раньше
    # персонаж каменел ровно тогда, когда на него смотрят в упор.
    сцэ = sc.Сценарист(random.Random(11))
    живые = {}
    for э in ("слушаю", "думаю", "спокоен", "огорчён", "встревожен"):
        сэ = {"эмоция": э, "человек": True, "взгляд": 0.0, "музыка": {"играет": False}}
        живые[э] = bool(сцэ._фоны(сэ, 100.0, 14))
    check("слушая и думая персонаж продолжает жить фоном",
          (живые["слушаю"], живые["думаю"], живые["спокоен"]), (True, True, True))
    check("короткие эмоции по-прежнему отдают экран своей сценке",
          (живые["огорчён"], живые["встревожен"]), (False, False))

    # --- привыкание к повтору и цикличные сценки -------------------------------
    # Цикличная сценка раньше крутилась одна до конца фона: за десять минут
    # музыки играл ровно ОДИН танец из восьми.
    сцм = sc.Сценарист(random.Random(5))
    музыка = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
              "музыка": {"играет": True, "название": "песня"}}
    танцы, прошлая = set(), None
    for i in range(600 * 10):
        к = сцм.кадр(музыка, i / 10, час=14)
        if к.get("имя") and к["имя"] != прошлая:
            танцы.add(к["имя"])
        прошлая = к.get("имя")
    check("за десять минут музыки пляшет не одна сценка, а почти все", len(танцы) >= 6, True)
    сцп = sc.Сценарист(random.Random(5))
    пусто = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    счёт, прошлая = {}, None
    for i in range(3600 * 5):
        к = сцп.кадр(пусто, i / 5, час=14)
        if к.get("имя") and к["имя"] != прошлая:
            счёт[к["имя"]] = счёт.get(к["имя"], 0) + 1
        прошлая = к.get("имя")
    самая_частая = max(счёт.values())
    check("за час в одиночестве ни одна сценка не лезет вперёд остальных",
          (len(счёт) >= 30, самая_частая <= 20), (True, True))
    # Привыкание считает именно повторы: сценка, сыгранная трижды, весит меньше.
    сцв = sc.Сценарист(random.Random(1))
    проба = sc.ПО_ИМЕНИ["зевнул"]
    полный = сцв._вес(проба, 0.0)
    сцв._сцена = проба
    for k in range(3):
        сцв._отметить(k * (проба.отдых * 2 + 1))
    после = сцв._вес(проба, проба.отдых * 6 + 10)
    check("трижды сыгранная сценка весит заметно меньше несыгранной",
          (полный == проба.вес, после < полный * 0.5), (True, True))
    # А со временем повтор ПРОЩАЕТСЯ: без этого сценка, сыгранная утром, к
    # вечеру всё ещё была бы в опале, и библиотека медленно вымирала бы.
    забыто = сцв._вес(проба, проба.отдых * 6 + sc.ПРИВЫКАНИЕ_ЗАБЫВАЕТСЯ * 8)
    check("через час-другой повторы прощаются и сценка возвращается в полную силу",
          забыто > полный * 0.9, True)
    # Перебитая событием сценка тоже считается сыгранной — иначе её отдых не
    # начинается и она может выпасть снова тут же.
    сцпр = sc.Сценарист(random.Random(4))
    рядом2 = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
              "музыка": {"играет": False}}
    for i in range(40):
        сцпр.кадр(рядом2, i / 10, час=14)
    идёт = сцпр._сцена
    имя_идущей = идёт.имя if идёт else ""
    # Музыка заиграла — событие «музыка_началась» перебивает фоновую сценку.
    сцпр.кадр(dict(рядом2, музыка={"играет": True, "название": "п"}), 4.1, час=14)
    check("перебитая сценка отмечена сыгранной — её отдых пошёл",
          (bool(имя_идущей), имя_идущей in сцпр._играли), (True, True))

    # --- ритм пауз неровный, а не метрономный ----------------------------------
    def разброс(числа):
        ср = sum(числа) / len(числа)
        return ср, (sum((ч - ср) ** 2 for ч in числа) / len(числа)) ** 0.5 / ср
    сцр = sc.Сценарист(random.Random(2))
    паузы = [сцр._пауза(sc.ПАУЗА_ПОКОЯ) for _ in range(4000)]
    ровный = random.Random(2)
    ровно = [ровный.uniform(*sc.ПАУЗА_ПОКОЯ) for _ in range(4000)]
    ср_нов, cv_нов = разброс(паузы)
    ср_ст, cv_ст = разброс(ровно)
    check("темп в среднем тот же, что был при ровном жребии",
          abs(ср_нов - ср_ст) < 0.6, True)
    check("а неровность выросла хотя бы в полтора раза — это ритм существа, не метронома",
          cv_нов > cv_ст * 1.5, True)
    check("изредка бывает пауза длиннее прежнего потолка, но не безразмерная",
          (max(паузы) > sc.ПАУЗА_ПОКОЯ[1], max(паузы) <= sc.ПАУЗА_ПОКОЯ[1] * sc.ПАУЗА_ПОТОЛОК),
          (True, True))
    check("пауза никогда не короче нижней границы", min(паузы) >= sc.ПАУЗА_ПОКОЯ[0], True)

    # --- план со стороны (режиссёр): подсказка, не приказ ---------------------
    сцпл = sc.Сценарист(random.Random(71))
    check("план принимает только существующие имена, по порядку, без повторов",
          сцпл.задать_план(["стучит по стеклу", "Выдумка", "зевнул", "стучит по стеклу"], "Ну же!"),
          ["стучит по стеклу", "зевнул"])
    check("план виден снаружи (копией)", сцпл.план, ["стучит по стеклу", "зевнул"])
    сцпл.кадр(покой, 0.0)
    for i in range(1, int(sc.ВНИМАНИЕ_ЧЕРЕЗ * 10) + 5):
        сцпл.кадр(рядом, i / 10)
    # Дальше — зов внимания: план кладём сейчас, как это делает режиссёр —
    # по нынешней обстановке. «Стучит по стеклу» уместен, «зевнул» (покой
    # без человека) — нет, и выбрасывается, а не ждёт, пока человек уйдёт.
    сцпл.задать_план(["стучит по стеклу", "зевнул"], "Ну же!")
    # СРОК ЗДЕСЬ С ЗАПАСОМ, И ВОТ ПОЧЕМУ. План кладётся посреди жизни, а
    # жребий к тому времени мог начать ЦЕПОЧКУ — историю из двух-трёх звеньев
    # («подкрался к краю → выглянул хитро → юркнул обратно»). Цепочку план не
    # перебивает и перебивать не должен: оборванная на середине история хуже
    # отложенной на пять секунд. Значит, план ждёт её конца — и проверка
    # обязана ждать столько же, иначе она проверяет не план, а везение.
    сыграно = []
    t = sc.ВНИМАНИЕ_ЧЕРЕЗ + 0.5
    while t < sc.ВНИМАНИЕ_ЧЕРЕЗ + 40.0 and len(сыграно) < 6:
        р = сцпл.кадр(рядом, round(t, 3))
        if р["имя"] and (not сыграно or сыграно[-1][0] != р["имя"]):
            сыграно.append((р["имя"], р["текст"]))
        t += 0.1
    check("фоновая из плана играется, и с репликой режиссёра поверх своей",
          ("стучит по стеклу", "Ну же!") in сыграно, True)
    check("неуместное из плана («зевнул» — не при человеке) выброшено, план кончился",
          (сцпл.план, any(и == "зевнул" for и, _ in сыграно)), ([], False))
    check("реплика режиссёра — только у одной сценки плана, дальше свои",
          sum(1 for _, т in сыграно if т == "Ну же!"), 1)
    check("годные: только фоны сейчас и отдохнувшие; в речи — пусто",
          (all(sc.ПО_ИМЕНИ[и].когда in ("зовёт_внимание", "человек_рядом") for и in сцпл.годные(рядом, t)),
           сцпл.годные(dict(рядом, говорит="х"), t)), (True, []))
    недавние = сцпл.недавние(5)
    check("недавние — сыгранные, свежие первыми (последняя может ещё идти)",
          (сыграно[0][0] in недавние, сыграно[1][0] in недавние,
           недавние.index(сыграно[0][0]) > недавние.index(сыграно[1][0])), (True, True, True))
    сцпл2 = sc.Сценарист(random.Random(72))
    сцпл2.кадр(покой, 0.0)
    сцпл2.задать_план(["зевнул"], "Ай")
    тр = сцпл2.кадр(dict(покой, эмоция="встревожен"), 0.1)
    check("событие важнее плана: тревога идёт, план ждёт",
          (sc.ПО_ИМЕНИ[тр["имя"]].когда if тр["имя"] else "", сцпл2.план), ("тревога", ["зевнул"]))
    check("пустой план — реплика не хранится", (sc.Сценарист().задать_план([], "x"),
                                                sc.Сценарист()._реплика_плана), ([], ""))

    # --- аватар отдаёт сценку странице ---------------------------------------
    аватар = Аватар()
    аватар.принять({"эмоция": "спокоен", "человек": False})
    аватар.принять({"эмоция": "спокоен", "человек": True, "взгляд": 0.0})
    с = аватар.состояние()
    check("в состоянии для страницы есть сценка с параметрами",
          ("сцена" in с, isinstance(с["сцена"].get("параметры"), dict)), (True, True))

    # --- диагностика: «человечек ничего не делает» — по логу, а не гаданию ---
    # Живая беда с форума: сценарист может молчать по трём разным причинам
    # (упал — уже в логе строкой выше; между сценками пауза — это нормально;
    # правда ничего не выбирает — это баг), и снаружи их не различить без
    # счётчика. Раз в минуту в лог идёт, сколько сценок сыграно.
    ад = Аватар()
    ад._считать_сценку("", 1000.0)
    check("пустое имя (сценки нет) не считается за сыгранную",
          ад._сцен_сыграно, 0)
    ад._считать_сценку("зевнул", 1001.0)
    ад._считать_сценку("зевнул", 1001.1)
    ад._считать_сценку("зевнул", 1001.2)
    check("та же сценка кадр за кадром — это одна сценка, не три",
          ад._сцен_сыграно, 1)
    ад._считать_сценку("потянулся", 1002.0)
    check("другое имя — вторая сценка", ад._сцен_сыграно, 2)
    журнал: list[str] = []
    старый_log_info = kuzya_pc.log.info
    kuzya_pc.log.info = lambda *a: журнал.append(a[0] % a[1:] if a[1:] else a[0])
    try:
        ад._считать_сценку("потянулся", 1000.0 + Аватар.ДИАГНОСТИКА_РАЗ_В)
    finally:
        kuzya_pc.log.info = старый_log_info
    check("через минуту — строка в лог со счётом, и счёт обнуляется",
          (len(журнал) == 1 and "сценок за" in журнал[0] and "2" in журнал[0],
           ад._сцен_сыграно), (True, 0))

    # --- страница (index.html) складывает сценку с позой правильно -----------
    # Поведение страницы гоняется в настоящем Chromium (pc/page_selftest.py);
    # здесь — по тексту, чтобы сторож был и в этом прогоне, без браузера:
    # рот сценки («зевнул», «свистит») ставится вместе с ртом позы, кто шире
    # — а не затирается им; и всё, что сценка когда-либо сдвигала, каждый
    # кадр стремится к нулю, а не остаётся «запечённым» после неё.
    страница = (_Path(__file__).resolve().parent / "avatar" / "index.html").read_text(encoding="utf-8")
    check("страница: рот сценки не затирается ртом позы (берётся больший)",
          "Math.max(поза.рот || 0, рот_сцены)" in страница, True)
    check("страница: прошлые сдвиги сценки каждый кадр обнуляются, а не остаются",
          "for (const id in наше) цель[id] = 0;" in страница, True)
    check("страница: запросы /state не копятся — один в полёте",
          "if (опрос_в_полёте) return;" in страница, True)

    # --- рассеянный взгляд: в покое голова и глаза не стоят чучелом ------------
    # Настоящий прогон (браузер, page_selftest.py: значения ParamAngleX/
    # ParamEyeBallY реально меняются кадр от кадра) — здесь по тексту, без
    # браузера. Жалоба, из-за которой это появилось: «двигается просто
    # вверх-вниз-влево-вправо, нет ощущения живого персонажа» — а на Live2D
    # между редкими сценками голова вообще не двигалась, глаза смотрели в
    # одну точку.
    # Контакта глаз в списке глушителей больше нет: там взгляд не замирает,
    # а идёт вчетверть силы (character.py, ВЗГЛЯД_ОТВОД) — неотрывный взгляд
    # читается как жуткий, а не как внимательный.
    check("страница: рассеянный взгляд есть и выключается на танце и взгляде сценки",
          "!танцует && typeof сцена.к !== \"number\"" in страница, True)
    check("страница: на контакте глаз блуждание НЕ выключается",
          "доля_наклона === 0 &&" in страница, False)
    # ЗНАК ВЗГЛЯДА — текстом, а не только поведением. Настоящая проверка
    # («человек СЛЕВА → голова к нему») живёт в page_selftest.py, но его
    # гоняет человек и CI, а свип мутаций — нет: для целей по index.html
    # сторож здесь. Без этой строки перевёрнутый знак ломался незамеченным —
    # а это ровно та поломка, из-за которой персонаж «не следил» за человеком.
    check("страница: взгляд на человека — В ЕГО сторону (минус: пеленг влево + , а у Cubism + это вправо)",
          ('|| 0) - доля_взгляда * 30;' in страница, '|| 0) - доля_взгляда;' in страница),
          (True, True))
    # Анимация покоя — самое заметное на экране, и она молча не играет, если
    # у модели группа названа не «Idle». Живая жалоба была именно такой:
    # «анимация около нуля» при полностью рабочем конвейере.
    check("страница: покой заводится сам, даже если группа у модели названа иначе",
          ("менеджер.groups.idle = покой_группа" in страница,
           "найти(группы, /^idle$/i, /idle/i)" in страница), (True, True))
    # Именно console.error, а не info/debug: в журнал ПК из консоли страницы
    # попадают только ошибки и предупреждения (kuzya_pc), а сказанное шёпотом
    # остаётся в консоли невидимого браузера — то есть нигде.
    check("страница: модель без движений — это сказано ошибкой, её видно в журнале ПК",
          'console.error("у модели «" + настройки.модель + "» нет ни одной группы ' in страница,
          True)
    check("страница: блуждание берётся из общей логики (поза «рассеян_x/y»), а не своё второе",
          ("Number(поза.рассеян_x)" in страница, "Number(поза.рассеян_y)" in страница,
           "Math.sin(t * 2 * Math.PI * 0.07)" not in страница), (True, True, True))

    # --- готовые модели в samples/ и старый формат Cubism 2 ---------------------
    # Настоящий прогон — page_selftest.py (страницы 12–14). Здесь по тексту и
    # по файлам: модели на месте (после git pull они должны появиться сами),
    # страница выбирает ядро по имени файла, старые имена параметров
    # переводятся в одном месте, рот берётся из группы LipSync модели, жесты
    # в покое есть и не лезут поверх речи/сна/танца/сценки.
    папка = _Path(__file__).resolve().parent / "avatar"
    check("samples/: Shizuku (Cubism 2), Mao (Cubism 4) и Mark (Cubism 4) лежат "
          "в репозитории со всем нужным",
          ((папка / "samples/shizuku/shizuku.model.json").is_file(),
           (папка / "samples/shizuku/moc/shizuku.moc").is_file(),
           (папка / "samples/shizuku/shizuku.physics.json").is_file(),
           (папка / "samples/mao/Mao.model3.json").is_file(),
           (папка / "samples/mao/Mao.moc3").is_file(),
           (папка / "samples/mao/Mao.physics3.json").is_file(),
           (папка / "samples/mark/mark_free_t04.model3.json").is_file(),
           (папка / "samples/mark/mark_free_t04.moc3").is_file(),
           (папка / "samples/mark/mark_free_t04.physics3.json").is_file(),
           (папка / "samples/mark/mark_free_t04.cdi3.json").is_file(),
           (папка / "samples/mark/mark_free_t04.2048/texture_00.png").is_file(),
           (папка / "samples/README.md").is_file()), (True,) * 12)
    import json
    with open(папка / "config.json", encoding="utf-8") as f:
        общий = json.load(f)
    # Живая поломка: config.json когда-то показывал по умолчанию на Shizuku
    # (аниме-девушку из samples/), и КАЖДЫЙ, кто просто запускал kuzya_pc.py
    # без единой своей настройки, получал на экране робота не Кузю, а её —
    # молча. Теперь модель по умолчанию пустая, и это единственно верное
    # значение: пусто — значит, домовёнка рисует сам робот, как исходно.
    check("config.json: модель по умолчанию — ПУСТАЯ, никто не подсовывает "
          "чужого персонажа без спроса",
          общий.get("модель", "нет ключа"), "")

    # --- модель_настроена(): съёмка не стартует без своей модели ---------------
    # Раньше kuzya_pc.py запускал headless-браузер ВСЕГДА (если стоит
    # Playwright), а модель в config.json была указана заранее (Shizuku) —
    # значит, любой запуск без единой строчки своих настроек снимал и слал
    # роботу чужого персонажа. Теперь решение «снимать ли вообще» принимается
    # ДО открытия браузера, отдельной чистой функцией — её и проверяем.
    with tempfile.TemporaryDirectory() as корень:
        корень = _Path(корень)
        # Приманка ВНЕ папки аватара — настоящий файл, чтобы «побег через ..»
        # проверялся по-честному: если бы защита молчала, файл на диске есть
        # и «нашёл» вышло бы true. С защитой не важно, что он есть — путь
        # снаружи папки в принципе не считается моделью.
        (корень / "секрет.txt").write_text("не модель", encoding="utf-8")
        врем = корень / "avatar"
        врем.mkdir()
        (врем / "config.json").write_text(json.dumps({"модель": "", "модель3d": ""}),
                                          encoding="utf-8")
        check("нет ни своей, ни общей модели — съёмку не с чего начинать",
              kuzya_pc.модель_настроена(врем), (False, ""))

        (врем / "config.local.json").write_text(
            json.dumps({"модель": "виджет.model.json"}), encoding="utf-8")
        check("своя модель вписана, но файла ещё нет на диске — тоже не старт",
              kuzya_pc.модель_настроена(врем), (False, "виджет.model.json"))

        (врем / "виджет.model.json").write_text("{}", encoding="utf-8")
        check("файл появился — теперь можно снимать",
              kuzya_pc.модель_настроена(врем), (True, "виджет.model.json"))

        # Побег из папки — «нет модели», даже если сам файл физически
        # существует где-то на диске: страница его всё равно не получит
        # (сервер наружу из avatar/ не отдаёт).
        (врем / "config.local.json").write_text(
            json.dumps({"модель": "../секрет.txt"}), encoding="utf-8")
        check("путь наружу из папки аватара — «нет модели», хотя файл и существует",
              kuzya_pc.модель_настроена(врем), (False, "../секрет.txt"))

        # 3D: поле «модель3d», а не «модель» — переключается по «движку».
        (врем / "config.local.json").write_text(
            json.dumps({"движок": "3d", "модель3d": "кузя.vrm"}), encoding="utf-8")
        check("движок 3d — смотрим на поле «модель3d», не «модель»",
              kuzya_pc.модель_настроена(врем), (False, "кузя.vrm"))
        (врем / "кузя.vrm").write_bytes(b"glTF")
        check("…и она тоже находится, если файл на месте",
              kuzya_pc.модель_настроена(врем), (True, "кузя.vrm"))
    check("страница: ядро и мост выбираются по имени файла модели (.model.json → Cubism 2)",
          ("cubism2 ? СКРИПТЫ_CUBISM2 : СКРИПТЫ_CUBISM4" in страница,
           "function старый_формат(" in страница), (True, True))
    check("страница: старые имена параметров — переводом в одном месте, с псевдонимами Shizuku",
          ("function имя_cubism2(" in страница, "PARAM_BODY_ANGLE_X: [\"PARAM_BODY_X\"]" in страница,
           "ядро.setParamFloat(имя, v)" in страница), (True, True, True))
    check("страница: рот — параметр из config, если он у модели есть, иначе из группы LipSync",
          "const РОТ = (п.рот && есть_параметр(п.рот)) ? п.рот : (липсинк || п.рот || \"ParamMouthOpenY\");"
          in страница, True)
    check("страница: жесты в покое не лезут поверх сценки, речи, сна и танца",
          'const занят = !!сцена.имя || !!с.говорит || с.эмоция === "сплю" || поза.метка === "танцует";'
          in страница, True)
    исходник_pc = (_Path(__file__).resolve().parent / "kuzya_pc.py").read_text(encoding="utf-8")
    check("kuzya_pc: файлы старого формата (.moc, .mtn) отдаются как двоичные",
          ('".moc": "application/octet-stream"' in исходник_pc, '".mtn": "application/octet-stream"' in исходник_pc),
          (True, True))

    # --- 3D-персонаж (VRM): выбор движка и мост сценок в кости --------------
    # Поведение страницы гоняется в настоящем Chromium с настоящей моделью
    # (page_selftest.py, «3D: …»); здесь — то, что проверяется без браузера:
    # выбор движка, библиотеки на месте, и главное — что сценки переводятся
    # в кости. Без этого моста все 192 сценки на 3D были бы немыми, ровно как
    # они были немыми на своём экране робота, пока не перевели их в глаза.
    страница3d = (папка / "3d.html").read_text(encoding="utf-8")
    check("3D: страница и библиотеки лежат в репозитории — работает без интернета",
          ((папка / "3d.html").is_file(),
           (папка / "vendor" / "three.module.min.js").is_file(),
           (папка / "vendor" / "three-vrm.module.min.js").is_file(),
           (папка / "vendor" / "loaders" / "GLTFLoader.js").is_file()),
          (True,) * 4)
    check("3D: сценки переводятся в кости (голова, корпус, руки), а не пропадают",
          ('повернуть("head"' in страница3d, 'повернуть("spine"' in страница3d,
           'повернуть("leftUpperArm"' in страница3d, 'п["ParamAngleX"]' in страница3d),
          (True,) * 4)
    check("3D: моргание своё — в VRM автоматического нет, а немигающий взгляд мёртв",
          "function моргание(" in страница3d, True)
    # ЗНАКИ ВЗГЛЯДА — текстом, по той же причине, что и у index.html: живая
    # проверка («человек слева → и голова, и глаза влево») живёт в
    # page_selftest.py с настоящим Chromium, а свип мутаций его не гоняет.
    # Пеленг положителен, когда человек СЛЕВА, мир three.js растёт вправо —
    # значит, минус. Без этих строк персонаж молча отворачивался от того, на
    # кого якобы смотрит, а голова к человеку не поворачивалась вовсе.
    check("3D: взгляд на человека — В ЕГО сторону (минус у пеленга)",
          "const гляжу_x = -доля_взгляда" in страница3d, True)
    check("3D: голова поворачивается к человеку, а не только зрачки",
          ("- доля_взгляда * ГОЛОВА_К_ЧЕЛОВЕКУ" in страница3d,
           "голова_y," in страница3d), (True, True))
    # Разборщик моделей: по нему человек за секунду понимает, годится ли
    # скачанная с VRoid Hub модель, не открывая браузер. Проверяем на нашей
    # же пробе (её собирает vrm_проба.py) и на подделке — молчать о плохом
    # файле он не имеет права, иначе им незачем пользоваться.
    import vrm_проба
    import vrm_проверить
    import struct as _struct
    _проба_glb = vrm_проба.собрать()
    _длина_json = _struct.unpack("<I", _проба_glb[12:16])[0]
    разбор = vrm_проверить.разобрать(json.loads(_проба_glb[20:20 + _длина_json]))
    check("разборщик моделей видит выражения, кости и лицензию",
          (sorted(разбор["выражения"])[:3], len(разбор["кости"]),
           разбор["лицензия"]["кем можно играть"], разбор["версия"]),
          (["aa", "angry", "blink"], 15, "everyone", "VRM 1.0"))
    беда_разбора = ""
    try:
        vrm_проверить.разобрать({"asset": {"version": "2.0"}})   # glTF без VRM
    except ValueError as e:
        беда_разбора = str(e)
    check("…и честно говорит, когда это вообще не VRM, а обычная 3D-модель",
          "нет расширения VRM" in беда_разбора, True)
    check("3D: физика волос и одежды считается каждый кадр (vrm.update)",
          "модель.update(dt)" in страница3d, True)
    выбор = _Path(_tf.mkdtemp())
    (выбор / "config.local.json").write_text('{"движок": "3d"}', encoding="utf-8")
    какая_3d = kuzya_pc.страница_аватара(выбор)
    (выбор / "config.local.json").write_text('{"движок": "ерунда"}', encoding="utf-8")
    какая_ерунда = kuzya_pc.страница_аватара(выбор)
    check("движок выбирается в config.local.json; незнакомое слово и отсутствие файла — "
          "это Live2D, а не пустой экран",
          (какая_3d, какая_ерунда, kuzya_pc.страница_аватара(_Path(_tf.mkdtemp()))),
          ("3d.html", "", ""))
    check("kuzya_pc снимает ту страницу, которую выбрали, а не всегда Live2D",
          "/avatar/{страница_аватара()}" in исходник_pc, True)
    # Съёмка headless-браузером не должна стартовать «на всякий случай»:
    # решение принимает модель_настроена() ДО импорта Playwright и создания
    # Съёмка — иначе (как было) любой запуск без своих настроек снимал и
    # слал роботу заглушку из config.json по умолчанию.
    check("аватар на ПК не настроен — съёмка вовсе не стартует, а не показывает заглушку",
          ("задана, путь_модели = модель_настроена()" in исходник_pc,
           "        if not задана:" in исходник_pc), (True, True))
    check("…и решение об этом принято ДО открытия браузера",
          исходник_pc.index("задана, путь_модели = модель_настроена()")
          < исходник_pc.index("съёмка = Съёмка("), True)


    # --- живая беда: человечек «просто дышит», рот не открывает, 0 действий --
    # Настоящий прогон (браузер, page_selftest.py: «мусорная строка вместо
    # числа не превращается в NaN» и «мусор в ParamMouthOpenY…») — здесь,
    # как и выше, только по тексту. Причина живой была именно в этом: одно
    # нечисловое значение параметра (битый config.json, опечатка в сценке)
    # через экспоненциальное сглаживание превращалось в NaN и оставалось
    # им НАВСЕГДА (NaN*k+NaN=NaN) — а рот и жесты незаметно переставали
    # реагировать вовсе, при живой, дышащей собственной анимацией модели.
    check("страница: мусорное число в общем цикле параметров гасится, не копится в NaN",
          "if (!Number.isFinite(цель[id])) цель[id] = плавно[id] || 0;" in страница, True)
    check("страница: мусорное число в ParamMouthOpenY сценки не превращает рот в NaN",
          "if (!Number.isFinite(рот_сцены)) рот_сцены = 0;" in страница, True)
    check("страница: ошибка внутри кадра не оставляет оверлей сломанным навсегда "
          "(сброс сглаживания) и видна на экране/в логе, а не тонет молча",
          ("for (const id in плавно) delete плавно[id];" in страница,
           "показать(текст);" in страница), (True, True))
    исходник_pc = (_Path(__file__).resolve().parent / "kuzya_pc.py").read_text(encoding="utf-8")
    check("ПК: страница слушает свои же JS-ошибки (pageerror, console.error) — "
          "иначе такая поломка не видна нигде, ни на экране, ни в логе",
          ('страница.on("pageerror"' in исходник_pc, 'страница.on("console"' in исходник_pc),
          (True, True))


def test_director() -> None:
    """Режиссёр (face/director.py): модель ПК выбирает сценки — как подсказку.

    Проверяется без Ollama: вопрос собирается по обстановке, ответ
    разбирается из чего угодно (JSON, текст, мусор), чужие имена и длинные
    реплики не проходят, спрашивает только в покое и не чаще срока, а на ПК
    (Аватар) вопрос уходит в свой поток и не лезет в очередь во время
    разговора.
    """
    section("режиссёр: модель выбирает сценки")
    import json
    import random
    import sys as _sys
    import time
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "face"))
    import director as dr
    import scenes as sc

    покой = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    рядом = dict(покой, человек=True, взгляд=0.0)
    сц = sc.Сценарист(random.Random(81))
    сц.кадр(покой, 0.0)
    for i in range(1, 200):
        сц.кадр(рядом, i / 10)

    # --- обстановка и вопрос ------------------------------------------------
    строки = dr.обстановка(dict(рядом, погода={"t": 12.0, "код": 61, "описание": "дождь"},
                                таймеры=[{"имя": "чай", "осталось": 200}], подсказка="Скажи: «Кузя»",
                                батарея=10.2, ночь=True), сц, 20.0, час=23, минута=5)
    check("обстановка словами: время и ночь, человек молчит N с, карточка, таймер, батарея, подсказка",
          ("23:05" in строки[0] and "ночь" in строки[0], "молчит уже" in строки[1],
           "+12°, дождь" in строки[2], "«чай», осталось 3:20" in строки[2],
           "батарея почти села" in строки[2], "подсказка" in строки[2]),
          (True, True, True, True, True, True))
    check("никого нет — так и сказано; недавние сценки перечислены",
          ("никого" in dr.обстановка(покой, сц, 20.0)[1],
           any(с.startswith("уже сыграно") for с in dr.обстановка(покой, сц, 20.0))), (True, True))
    check("время суток: утро/день/вечер/ночь",
          [dr.время_суток(ч) for ч in (7, 13, 19, 2)], ["утро", "день", "вечер", "ночь"])
    вопрос = dr.запрос(рядом, сц, 20.0, 19, 40)
    check("вопрос: обстановка, список уместных имён, просьба про JSON и лимит реплики",
          ("Обстановка" in вопрос, "Сценки:" in вопрос, "JSON" in вопрос,
           f"до {dr.РЕПЛИКА_НЕ_ДЛИННЕЕ} знаков" in вопрос,
           all(и in вопрос for и in сц.годные(рядом, 20.0, 19)[:dr.ИМЁН_В_СПИСКЕ])),
          (True, True, True, True, True))
    check("в речи и в танце вопроса нет — нечего выбирать",
          (dr.запрос(dict(рядом, говорит="х"), сц, 20.0),
           dr.запрос(dict(рядом, музыка={"играет": True}), сц, 20.0)), ("", ""))
    check("список имён в вопросе ограничен ИМЁН_В_СПИСКЕ",
          вопрос.count(";") + 1 <= dr.ИМЁН_В_СПИСКЕ + 2, True)

    # --- в показе модели — ГЛАВНЫЙ фон, а не первые сорок строк файла -------
    # Режиссёр видит только ИМЁН_В_СПИСКЕ имён: весь список в запрос не лезет.
    # Пока годные шли в порядке объявления, «первые сорок» значило «те, что
    # раньше написаны». С ростом библиотеки это стало ложью: в режиме зова
    # годных больше сотни, а сами зовы объявлены в конце файла — режиссёр их
    # не видел вовсе и «звал» человека спокойными сценками, то есть не звал.
    сцз = sc.Сценарист(random.Random(3))
    покой_з = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    рядом_з = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
               "музыка": {"играет": False}}
    сцз.кадр(покой_з, 0.0)
    т = 0.1
    while т < sc.ВНИМАНИЕ_ЧЕРЕЗ + 5.0:          # доводим до режима зова
        сцз.кадр(рядом_з, round(т, 3))
        т += 0.1
    годные_з = сцз.годные(рядом_з, т, 12)
    показ = годные_з[:dr.ИМЁН_В_СПИСКЕ]
    зовов = sum(1 for и in показ if sc.ПО_ИМЕНИ[и].когда == "зовёт_внимание")
    check("зовёт внимание — и режиссёру показаны именно зовы, а не хвост покоя",
          (len(годные_з) > dr.ИМЁН_В_СПИСКЕ, зовов >= dr.ИМЁН_В_СПИСКЕ - 5),
          (True, True))
    # Внутри фона вперёд идут те, кого дольше не показывали: срез меняется сам
    # по мере игры, и модель каждый раз видит другой набор.
    сыгранные = [и for и in годные_з if и in сцз._играли]
    if сыгранные:
        места = [годные_з.index(и) for и in сыгранные]
        свежайший = max(сыгранные, key=lambda и: сцз._играли[и])
        check("недавно сыгранная — в хвосте показа, а не впереди",
              годные_з.index(свежайший) >= max(места) - len(сыгранные), True)

    # --- разбор ответа: что угодно, а на экран — только своё ----------------
    годные = ["стучит по стеклу", "машет обеими: эй", "кувырок", "зевнул", "прыгает, чтобы заметили"]
    check("JSON как просили: имена по порядку, чужое выброшено, реплика причёсана",
          dr.разобрать('Конечно! {"сценки": ["кувырок", "Выдумка", "стучит по стеклу"], '
                       '"реплика": "Привет, малыш! 😀 \\"иди сюда\\""}', годные),
          (["кувырок", "стучит по стеклу"], "Привет, малыш! иди сюда"))
    check("английские ключи модели — тоже понимаем",
          dr.разобрать('{"scenes": ["зевнул"], "text": "Ну же"}', годные), (["зевнул"], "Ну же"))
    check("не JSON — имена по месту в тексте, реплике не верим",
          dr.разобрать("Сначала машет обеими: эй, потом кувырок. Скажет: привет", годные),
          (["машет обеими: эй", "кувырок"], ""))
    check("мусор — пустой план, не падение",
          (dr.разобрать("", годные), dr.разобрать("{{{", годные), dr.разобрать(None, годные)),
          (([], ""), ([], ""), ([], "")))
    check("не больше ПЛАН_ДЛИНА сценок",
          len(dr.разобрать(json.dumps({"сценки": годные * 2}, ensure_ascii=False), годные)[0]),
          dr.ПЛАН_ДЛИНА)
    check("реплика: смайлики и кавычки вычищены, длинная режется по слову с многоточием",
          (dr.причесать_реплику("«Ой!» 🎉"),
           len(dr.причесать_реплику("Очень длинная реплика, которая точно не влезет")) <= dr.РЕПЛИКА_НЕ_ДЛИННЕЕ + 1,
           dr.причесать_реплику("Очень длинная реплика, которая точно не влезет").endswith("…")),
          ("Ой!", True, True))
    check("реплика не по-русски или пустая — в пузырь не идёт",
          (dr.причесать_реплику("hello there"), dr.причесать_реплику("!!!"), dr.причесать_реплику(None)),
          ("", "", ""))
    check("реплика не длиннее РЕПЛИКА_НЕ_ДЛИННЕЕ",
          len(dr.причесать_реплику("а" * 100)) <= dr.РЕПЛИКА_НЕ_ДЛИННЕЕ + 1, True)

    # --- когда спрашивать ----------------------------------------------------
    р = dr.Режиссёр()
    check("в покое, без плана, впервые — пора", р.пора(рядом, сц, 20.0), True)
    check("в речи, в поездке, под музыку, не в покое — не пора",
          (р.пора(dict(рядом, говорит="х"), сц, 20.0), р.пора(dict(рядом, едет=True), сц, 20.0),
           р.пора(dict(рядом, музыка={"играет": True}), сц, 20.0),
           р.пора(dict(рядом, эмоция="думаю"), сц, 20.0)), (False, False, False, False))
    check("вопрос собрался и отмечен", bool(р.запрос(рядом, сц, 20.0, 19)), True)
    check("сразу второй раз — не пора; через ПЛАН_РАЗ_В — пора",
          (р.пора(рядом, сц, 21.0), р.пора(рядом, сц, 20.0 + dr.ПЛАН_РАЗ_В)), (False, True))
    check("человек ушёл — обстановка переломилась: пора уже через ПОСЛЕ_ПЕРЕМЕНЫ",
          (р.пора(покой, сц, 20.0 + dr.ПОСЛЕ_ПЕРЕМЕНЫ - 0.5), р.пора(покой, сц, 20.0 + dr.ПОСЛЕ_ПЕРЕМЕНЫ)),
          (False, True))
    принято, реплика = р.принять(сц, '{"сценки": ["стучит по стеклу", "нет такой"], "реплика": "Иди сюда"}', годные)
    check("принять: план у сценариста, реплика с ним; чужое не прошло",
          (принято, реплика, сц.план), (["стучит по стеклу"], "Иди сюда", ["стучит по стеклу"]))
    check("пока план не сыгран — не пора, даже если срок вышел",
          р.пора(рядом, сц, 20.0 + 2 * dr.ПЛАН_РАЗ_В), False)
    check("ничего годного в ответе — реплика тоже не берётся",
          р.принять(sc.Сценарист(), "ерунда", годные), ([], ""))
    check("годные не переданы — все имена библиотеки",
          р.принять(sc.Сценарист(), '{"сценки": ["зевнул"]}')[0], ["зевнул"])
    check("счётчики: вопросов и планов", (р.вопросов, р.планов), (1, 2))

    # --- на ПК: Аватар спрашивает в своём потоке, не во время разговора --------
    чч = [1000.0]
    ав = Аватар(часы=lambda: чч[0])
    ав._часы_дня = lambda: time.struct_time((2026, 9, 7, 19, 40, 0, 0, 1, 0))
    ав.сценарист = sc.Сценарист(random.Random(82))
    вопросы: list[str] = []

    def модель(вопрос: str) -> str:
        # Как настоящая: выбирает из списка, который ей показали.
        вопросы.append(вопрос)
        список = вопрос.split("Сценки: ", 1)[1].split(".\n", 1)[0].split("; ")
        return json.dumps({"сценки": список[:2], "реплика": "Эй, ты!"}, ensure_ascii=False)

    ав.спросить_режиссёра = модель
    ав.принять(dict(покой))
    ав.состояние()
    for i in range(1, 200):
        чч[0] = 1000.0 + i / 10
        ав.принять(dict(рядом))
        ав.состояние()

    def дождаться(условие, сколько=3.0):
        крайний = time.monotonic() + сколько
        while time.monotonic() < крайний:
            if условие():
                return True
            time.sleep(0.02)
        return условие()
    check("аватар спросил модель (в потоке) и положил план сценаристу",
          (дождаться(lambda: bool(ав.сценарист.план) or вопросы and not ав._режиссёр_думает),
           len(вопросы) >= 1, "стучит по стеклу" in ав.сценарист.план or ав.режиссёр.планов >= 1),
          (True, True, True))
    check("вопрос — настоящий: с обстановкой и списком", "Обстановка" in вопросы[0] and "Сценки:" in вопросы[0], True)
    # Во время разговора — молчит: очередь к модели одна.
    было = len(вопросы)
    ав.сценарист.задать_план([], "")
    ав.разговор_начался()
    for i in range(200, 600):
        чч[0] = 1000.0 + i / 10
        ав.принять(dict(рядом))
        ав.состояние()
    check("идёт разговор — режиссёр модель не дёргает", len(вопросы), было)
    ав.разговор_кончился()
    for i in range(600, 700):
        чч[0] = 1000.0 + i / 10
        ав.принять(dict(рядом))
        ав.состояние()
    check("разговор кончился — спросил снова", дождаться(lambda: len(вопросы) > было), True)
    check("разговор_кончился без начала — не уходит в минус",
          (Аватар().разговор_кончился(), Аватар()._разговоров), (None, 0))
    # Модель упала — план пуст, кадр цел.
    ав2 = Аватар(часы=lambda: чч[0])
    ав2._часы_дня = ав._часы_дня
    ав2.сценарист = sc.Сценарист(random.Random(83))

    def сломанная(вопрос: str) -> str:
        raise RuntimeError("Ollama не отвечает")

    ав2.спросить_режиссёра = сломанная
    ав2.принять(dict(покой))
    ав2.состояние()
    for i in range(1, 100):
        чч[0] = 2000.0 + i / 10
        ав2.принять(dict(рядом))
        с = ав2.состояние()
    check("модель не ответила — состояние отдаётся, план пуст, флаг «думает» снят",
          (isinstance(с.get("сцена"), dict), ав2.сценарист.план,
           дождаться(lambda: not ав2._режиссёр_думает)), (True, [], True))
    check("без режиссёра (спросить_режиссёра=None) ничего не спрашивается",
          Аватар.спросить_режиссёра, None)
    # Провода в kuzya_pc.py: разговор помечается вокруг ответа модели, режиссёр
    # подключён к той же Ollama, страница знает повод зова.
    исходник = (_Path(__file__).resolve().parent / "kuzya_pc.py").read_text(encoding="utf-8")
    check("kuzya_pc: /v1/messages помечает разговор начался/кончился (finally)",
          ("аватар.разговор_начался()" in исходник, "аватар.разговор_кончился()" in исходник),
          (True, True))
    check("kuzya_pc: режиссёр подключён к Ollama в main",
          "srv.avatar.спросить_режиссёра = спросить_режиссёра" in исходник, True)
    страница = (_Path(__file__).resolve().parent / "avatar" / "index.html").read_text(encoding="utf-8")
    check("страница: на зов внимания — готовое движение «тап» модели",
          "зовёт_внимание: тап" in страница, True)


def test_ollv_bridge() -> None:
    """Мост к Open-LLM-VTuber: очередь на одну фразу плюс свой WS-клиент.

    Опциональная надстройка (voice: включается OLLV_URL), которая не трогает
    ни свой аватар, ни сценарист. Проверяется отдельно от них: очередь —
    числами, WAV — по байтам заголовка, а рукопожатие и кадр — против
    настоящего сервера по протоколу (RFC 6455), поднятого здесь же на
    голом сокете, без реального Open-LLM-VTuber.
    """
    section("мост к Open-LLM-VTuber")
    import base64
    import hashlib
    import socket
    import struct
    import threading
    import time

    import ollv_bridge as мб

    # --- очередь: текст, эмоция → тег, звук --------------------------------
    ч = [10.0]
    м = мб.Мост("127.0.0.1:12393", часы=lambda: ч[0])
    check("до первой фразы — номер 0, текста нет", м.строка(), {"номер": 0, "текст": ""})
    м.готово("Привет!", "рад", b"\x10\x00\x20\x00", 22050)
    check("эмоция стала тегом OLLV, номер вырос", м.строка(), {"номер": 1, "текст": "[joy] Привет!"})
    м.готово("Не понял.", "не_понял", b"\x00\x00", 16000)
    check("другая эмоция — другой тег, номер снова вырос",
          м.строка(), {"номер": 2, "текст": "[surprise] Не понял."})
    м.готово("Просто говорю.", "неизвестная-эмоция", b"", 22050)
    check("неизвестное слово эмоции — тега нет, но текст цел",
          м.строка()["текст"], "Просто говорю.")
    звук = м.звук()
    check("WAV собран правильно: заголовок RIFF/WAVE, частота, данные на месте",
          (звук[:4], звук[8:12], struct.unpack("<I", звук[24:28])[0], звук[44:]),
          (b"RIFF", b"WAVE", 22050, b""))

    # --- кадр_текстом: чистая функция, проверяется по протоколу --------------
    кадр = мб.кадр_текстом("привет")
    check("кадр промаскирован (клиент обязан маскировать) — бит 0x80 в длине", кадр[1] & 0x80, 0x80)
    маска, тело = кадр[2:6], кадр[6:]
    check("размаскированный кадр — исходный текст в UTF-8",
          bytes(б ^ маска[i % 4] for i, б in enumerate(тело)), "привет".encode("utf-8"))
    длинная = мб.кадр_текстом("ф" * 200)
    check("кадр длиннее 125 байт — 16-битная длина (второй байт 126)", длинная[1] & 0x7F, 126)

    # --- рукопожатие и сигнал — против настоящего сервера по RFC 6455 --------
    ПРИВЯЗКА_WS = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    получено: list[str] = []
    сервер = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    сервер.bind(("127.0.0.1", 0))
    сервер.listen(1)
    порт = сервер.getsockname()[1]

    def фальшивый_ollv() -> None:
        соединение, _ = сервер.accept()
        with соединение:
            запрос = b""
            while b"\r\n\r\n" not in запрос:
                запрос += соединение.recv(4096)
            заголовки = запрос.decode("ascii", "replace")
            check("рукопожатие просит апгрейд до websocket, версию 13",
                  ("Upgrade: websocket" in заголовки, "Sec-WebSocket-Version: 13" in заголовки),
                  (True, True))
            ключ = next(с.split(":", 1)[1].strip() for с in заголовки.split("\r\n")
                       if с.lower().startswith("sec-websocket-key:"))
            принято = base64.b64encode(
                hashlib.sha1((ключ + ПРИВЯЗКА_WS).encode()).digest()).decode()
            соединение.sendall(
                (b"HTTP/1.1 101 Switching Protocols\r\n"
                 b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 + f"Sec-WebSocket-Accept: {принято}\r\n\r\n".encode()))
            голова = соединение.recv(2)
            длина = голова[1] & 0x7F
            маска = соединение.recv(4)
            замаскировано = соединение.recv(длина)
            получено.append(bytes(б ^ маска[i % 4] for i, б in
                                  enumerate(замаскировано)).decode("utf-8"))

    поток = threading.Thread(target=фальшивый_ollv, daemon=True)
    поток.start()
    м2 = мб.Мост(f"127.0.0.1:{порт}/client-ws")
    м2._послать_сигнал()
    поток.join(timeout=3.0)
    check("настоящий (по протоколу) сервер принял рукопожатие и прочитал наш кадр",
          получено, ['{"type": "ai-speak-signal"}'])
    сервер.close()

    # --- фоновый поток: будится, шлёт, не долбит без повода -------------------
    сигналов: list[int] = []
    м3 = мб.Мост("127.0.0.1:1")           # порт, где никто не слушает — только считаем попытки
    м3._послать_сигнал = lambda: sigнализировать(сигналов)

    def sigнализировать(лог):
        лог.append(1)

    м3.start()
    м3.готово("Тест", "спокоен", b"", 16000)
    срок = time.monotonic() + 2.0
    while len(сигналов) < 1 and time.monotonic() < срок:
        time.sleep(0.02)
    check("новая фраза будит фоновый поток — сигнал отправлен один раз", сигналов, [1])
    м3.stop()

    # --- провода в kuzya_pc.py: /tts кладёт фразу в мост, роуты подключены ---
    исходник = (Path(__file__).resolve().parent / "kuzya_pc.py").read_text(encoding="utf-8")
    check("kuzya_pc.py: /tts сообщает мосту готовую фразу тем же звуком, что и роботу",
          "мост.готово(" in исходник, True)
    check("kuzya_pc.py: HTTP-роуты /ollv/line и /ollv/audio подключены",
          ('"/ollv/line"' in исходник, '"/ollv/audio"' in исходник), (True, True))
    check("мост включается только переменной окружения (по умолчанию выключен, ничего не трогает)",
          'os.environ.get("OLLV_URL"' in исходник, True)


def добор_проверок(названные: tuple) -> list:
    """Проверки, дописанные в файл, но не вписанные в кортеж main() руками.

    ПОЧЕМУ ЭТО ЗАВЕЛОСЬ. Новая проверка стоила двух правок в разных концах
    файла: сама функция внизу и её имя в кортеже main(). Пока файл правит
    один человек — мелочь. Когда несколько работ подряд дописывают сюда своё,
    все они лезут в одну и ту же строку кортежа, и половина правок приезжает
    конфликтом — не по смыслу, а по тексту: два имени в одно место.

    Хуже другое: забытое имя не видно НИЧЕМ. Функция лежит в файле, читается
    как написанная проверка, проходит глазами при разборе — и не запускается
    никогда. В соседнем voice/selftest.py так и пролежала
    test_ears_default_by_device: тридцать строк разбора pult.html, ни разу не
    позванных. Нашлась она не падением — падать было некому, — а вот этой
    самой правкой, когда в globals заглянули впервые.

    Поэтому кортеж выше остаётся ручным и неприкосновенным: на его ПОРЯДКЕ
    стоят ожидания соседей (одна проверка поднимает сервер и кладёт заглушки,
    следующая на них считает). А всё остальное, что похоже на проверку,
    добирается следом за ним.

    «ПОХОЖЕ НА ПРОВЕРКУ» — ЭТО МАЛО, и потому здесь три отсева:

      • не функция: имя `test_` живёт и на числе, и на заглушке, которую одна
        проверка положила в globals ради соседней;
      • функция ЧУЖОГО модуля, приехавшая импортом: её гоняет её же файл, и
        звать её отсюда — значит считать одно и то же дважды и врать об
        охвате;
      • функция, которая просит доводы: это подручная для проверок рядом.
        Позови её без доводов — прогон умрёт на TypeError, не дойдя до
        настоящих проверок, и виноватым будет выглядеть кто угодно, кроме
        описки в имени.

    Отсев по доводам НЕ МОЛЧИТ: имя `test_` на подручной — почти всегда
    описка, и сказать о ней надо вслух, иначе человек будет искать свою
    проверку в выводе и не найдёт.

    Порядок добранных — по строке определения: прогон тогда читается сверху
    вниз вместе с файлом. globals() и так отдаёт имена в порядке появления,
    но это подробность устройства словаря, а не обещание, и опираться на неё
    нельзя.
    """
    import inspect

    видели = set(названные)
    добранные: list = []
    # list(): по globals ходим не в одиночестве — проверки поднимают потоки и
    # серверы, и словарь модуля во время обхода может подрасти. Снимок стоит
    # микросекунды, а RuntimeError посреди прогона стоит всего прогона.
    for имя, что in list(globals().items()):
        if not имя.startswith("test_") or not inspect.isfunction(что):
            continue
        if что.__module__ != __name__ or что in видели:
            continue
        просит = [п.name for п in inspect.signature(что).parameters.values()
                  if п.default is inspect.Parameter.empty
                  and п.kind not in (п.VAR_POSITIONAL, п.VAR_KEYWORD)]
        if просит:
            print(f"   (не зову {имя}: просит {', '.join(просит)} — "
                  f"похоже на подручную, а не на проверку)")
            continue
        видели.add(что)
        добранные.append(что)
    return sorted(добранные, key=lambda ф: ф.__code__.co_firstlineno)


def test_дописанная_проверка_не_остаётся_незапущенной() -> None:
    """Проверка, дописанная в файл, попадает в прогон сама — без правки main().

    ЗАЧЕМ. Самопроверка честна ровно настолько, насколько полон её список.
    Проверка, которую забыли вписать в main(), — это не «на одну меньше», это
    ХУЖЕ, чем её отсутствие: она лежит в файле, её видно при разборе, на неё
    ссылаются в обсуждении — и все считают механизм под присмотром. А под
    присмотром он не был ни минуты. Ровно так и вышло в соседнем
    voice/selftest.py: test_ears_default_by_device написана, доказательна и ни
    разу не позвана.

    Поэтому здесь три требования, и все три — к машине, а не к
    внимательности:

      • main() правда прогоняет добранное, а не только названное руками;
      • каждая проверка файла попадает в прогон, и ровно один раз;
      • то, что лишь ПОХОЖЕ на проверку, в прогон не попадает.

    Последнее — не педантизм. Подручная, позванная без доводов, роняет весь
    прогон на TypeError: самопроверка перестаёт проверять что-либо вообще, а
    выглядит это как поломка того файла, до которого прогон не дошёл.

    Часть проверки — разбором исходника, а не вызовом: вызвать main() отсюда
    нельзя (мы внутри неё), а вопрос «а зовёт ли она добор вообще» задать
    надо. Убрать добор из main и оставить сам добор целым — самая вероятная
    поломка: она выглядит как безобидное упрощение строки.
    """
    section("дописанная проверка запускается сама")

    import ast
    import inspect

    исходник = Path(__file__).resolve().read_text(encoding="utf-8")
    дерево = ast.parse(исходник)
    тело_main = [у for у in дерево.body
                 if isinstance(у, ast.FunctionDef) and у.name == "main"][0]

    # Кортеж берём разбором, а не из globals: в кортеже-объекте повтор имени
    # уже неразличим, а в тексте — виден. Проверка, вписанная дважды, гоняется
    # дважды и сбивает счёт заглушкам соседей.
    названные_имена: list = []
    for узел in ast.walk(тело_main):
        if isinstance(узел, ast.Assign) and isinstance(узел.value, ast.Tuple) \
                and any(isinstance(ц, ast.Name) and ц.id == "названные"
                        for ц in узел.targets):
            названные_имена = [э.id for э in узел.value.elts
                               if isinstance(э, ast.Name)]
    check("кортеж проверок в main() нашёлся разбором",
          len(названные_имена) >= 20, True)
    check("в кортеже main() нет повторов",
          sorted({и for и in названные_имена if названные_имена.count(и) > 1}),
          [])

    циклы = [у for у in ast.walk(тело_main) if isinstance(у, ast.For)]
    прогон = [ц for ц in циклы if "добор_проверок" in ast.dump(ц.iter)
              and "названные" in ast.dump(ц.iter)]
    check("main() прогоняет и добранные проверки, а не только названные",
          len(прогон), 1)
    зовут = bool(прогон) and isinstance(прогон[0].target, ast.Name) and any(
        isinstance(у, ast.Call) and isinstance(у.func, ast.Name)
        and у.func.id == прогон[0].target.id
        for у in ast.walk(прогон[0]))
    check("добранные не перечислены, а вызваны", зовут, True)

    названные = tuple(globals()[и] for и in названные_имена)
    добранные = добор_проверок(названные)
    имена_добранных = [ф.__name__ for ф in добранные]
    все_проверки = [у.name for у in дерево.body
                    if isinstance(у, ast.FunctionDef)
                    and у.name.startswith("test_")]
    check("ни одна проверка файла не остаётся незапущенной",
          sorted(set(все_проверки) - set(названные_имена)
                 - set(имена_добранных)), [])
    check("добранное не повторяет названное",
          sorted(set(названные_имена) & set(имена_добранных)), [])
    check("добранные идут в порядке появления в файле",
          [ф.__code__.co_firstlineno for ф in добранные],
          sorted(ф.__code__.co_firstlineno for ф in добранные))

    # А теперь подсовываем добору четыре подделки и одну настоящую проверку.
    # Настоящую он обязан взять — и ровно один раз, даже если на неё смотрят
    # два имени. Подделки — не взять ни одной.
    def test_свежая_проверка() -> None:
        """Как будто дописана только что: в кортеже её нет и не будет."""

    def подручная(данные):
        raise AssertionError("подручную позвали как проверку")

    чужой = types.ModuleType("чужой_модуль")
    exec("def test_из_чужого_модуля():\n    pass\n", чужой.__dict__)

    подделки = {
        "test_свежая_проверка": test_свежая_проверка,
        "test_свежая_проверка_вторым_именем": test_свежая_проверка,
        "test_подручная_просит_доводы": подручная,
        "test_вовсе_не_функция": 42,
        "test_из_чужого_модуля": чужой.test_из_чужого_модуля,
    }
    globals().update(подделки)
    try:
        с_подделками = добор_проверок(названные)
    finally:
        # Подделки обязаны исчезнуть ДО того, как main() позовёт добор
        # по-настоящему: иначе прогон закончится вызовом подручной, и
        # разбираться в этом будет тот, кто ни при чём.
        for и in подделки:
            globals().pop(и, None)
    имена_с_подделками = [ф.__name__ for ф in с_подделками]

    check("дописанная проверка добирается сама",
          "test_свежая_проверка" in имена_с_подделками, True)
    check("одна и та же проверка под двумя именами зовётся один раз",
          имена_с_подделками.count("test_свежая_проверка"), 1)
    check("подручная с доводами в добор не попадает",
          "подручная" in имена_с_подделками, False)
    check("чужая функция из импорта в добор не попадает",
          "test_из_чужого_модуля" in имена_с_подделками, False)
    check("не-функция с именем проверки в добор не попадает",
          [ф for ф in с_подделками if not inspect.isfunction(ф)], [])


def test_измеритель_не_врёт() -> None:
    """Линейка обязана уметь сказать, что всё плохо.

    Инструмент, который показывает благополучие всегда, — не линейка, а
    украшение: по нему нельзя принять ни одной работы. Поэтому скармливаем
    ему заведомо мёртвого персонажа — застывшую позу, одну сценку по кругу,
    чистую периодику — и требуем провала по каждой статье. Если линейка и
    тут скажет «хорошо», ей нельзя верить и в обратную сторону.
    """
    section("линейка живости честна")
    import math as _m
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import importlib.util as _iu
    путь = Path(__file__).resolve().parent.parent / "scripts" / "прогон_жизни.py"
    спец = _iu.spec_from_file_location("прогон_жизни", путь)
    лин = _iu.module_from_spec(спец)
    спец.loader.exec_module(лин)

    # Поза, которая не меняется вовсе, — это стоп-кадр, а не персонаж.
    мертво = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)] * 600
    check("застывшая поза — линейка показывает провал, а не ноль",
          лин._неподвижность(мертво) > 0.9, True)
    живо = [(round(_m.sin(и / 7.0), 4), 0.0, 0.0, round(_m.cos(и / 5.0), 4), 0.0, 0.0)
            for и in range(600)]
    check("…а меняющаяся поза провалом не считается",
          лин._неподвижность(живо), 0.0)

    # Чистая синусоида — предел механичности: ровно то, что глаз читает как
    # заводную игрушку.
    метроном = [_m.sin(и * 2 * _m.pi / 100.0) for и in range(3000)]
    check("метроном линейка узнаёт: предсказуемость у верхней границы",
          лин._предсказуемость(метроном) > 0.8, True)
    шум = [_m.sin(и / 3.3) * _m.cos(и / 17.7) + (и % 13) / 13.0 for и in range(3000)]
    check("…а у неровного движения она заметно ниже",
          лин._предсказуемость(шум) < лин._предсказуемость(метроном), True)

    # Одна сценка по кругу — это не разнообразие, сколько её ни повторяй.
    одна = [(и * 3.0, "зевнул") for и in range(200)]
    р = лин._разнообразие(одна)
    check("одна сценка по кругу — разных ровно одна, и вся доля у неё",
          (р["разных за час"], round(р["доля самой частой"], 3)), (1, 1.0))
    пусто = лин._разнообразие([])
    check("персонаж не сыграл ничего — линейка это видит, а не делит на ноль",
          (пусто["разных за час"], пусто["доля самой частой"]), (0, 1.0))

    # БЕЗ ПК ПЕРСОНАЖ НЕ ДОЛЖЕН ПРОСЕДАТЬ — и это надо МЕРИТЬ, а не обещать.
    # Флаг `--без-пк` у прибора долго принимался, доезжал до метрик отдельным
    # полем и не влиял ни на что: режиссёра прогон не звал ни в одном режиме.
    # Прибор честно печатал два одинаковых числа, и по ним можно было
    # заключить «без компьютера всё то же самое» — вывод правильный, но
    # полученный из ручки, которая ничего не крутит. Теперь с ПК режиссёр и
    # правда работает (модель заменена заглушкой: прибор меряет не качество
    # модели, а то, много ли теряет персонаж без компьютера).
    с_пк = лин.прогнать(суток=0.2, зерно=5)
    без_пк = лин.прогнать(суток=0.2, зерно=5, без_пк=True)
    check("режиссёр в прогоне с ПК и правда работает, а не числится",
          с_пк["_сценок всего"] != без_пк["_сценок всего"], True)
    просело = 1.0 - без_пк["разных за час"] / max(1, с_пк["разных за час"])
    check("без ПК разнообразие не проседает: персонаж живёт своим, а не чужим",
          просело <= 0.10, True)
    check("…и ночь без ПК остаётся ночью, а поводы отыгранными",
          (без_пк["ночь: доля дневных сценок"], без_пк["бедных поводов"]), (0.0, 0))


def test_застывшая_страница_не_считается_живой() -> None:
    """Марк замер, а кадры идут — и робот часами верит, что всё хорошо.

    Обман тонкий. Скринкаст Chrome шлёт кадр только на ИЗМЕНЕНИЕ картинки,
    поэтому у застывшей страницы кадров нет вовсе — и ровно на этот случай мы
    раз в секунду снимаем её обычным скриншотом. Скриншот приходит как
    нормальный кадр и обновляет отметку свежести, а значит обманывает ВСЕ
    сторожа разом: и здешний, и робота с его ПОТОК_СТАРЕЕТ. Страница может
    лежать с исключением в консоли, а на экране робота будет верно висеть её
    последний вздох — и откат к домовёнку не сработает никогда.

    Сторож стоит на ПК, а не на роботе: только здесь известно, что страница
    ОБЯЗАНА шевелиться. У робота повтор кадра — штатное дело.
    """
    section("ПК: застывшая страница видна")
    часы = [1000.0]
    с = kuzya_pc.Съёмка(часы=lambda: часы[0])

    с.положить(b"frame-1")
    часы[0] += 1.0
    с.положить(b"frame-2")
    свежесть_живого = с.когда
    for _ in range(6):                      # шесть секунд одного и того же
        часы[0] += 1.0
        с.положить(b"frame-2")
    check("одинаковый кадр не обновляет свежесть — кадр честно стареет",
          с.когда, свежесть_живого)
    check("…и про это сказано словами, а не молча",
          с.беда, "страница застыла")
    # Робот смотрит на возраст кадра: раз он не двигается, поток протухнет и
    # экран уйдёт на домовёнка — вместо вечного показа мертвеца.
    check("протухший кадр первым в поток не отдаётся",
          с.кадр(не_старше=с.СВЕЖИЙ)[0], b"")
    часы[0] += 1.0
    с.положить(b"frame-3")
    check("страница ожила — сторож снят, кадры снова свежие",
          (с.беда, с.когда), ("", часы[0]))


def test_персонаж_живёт_без_страницы() -> None:
    """Закрыли вкладку — и персонаж замер на полужесте.

    Сценарист на ПК двигался только тогда, когда кто-то дёргал /avatar/state:
    страница раз в сотую долю секунды, синтез — ради одного поля эмоции. Стоит
    вкладке закрыться или упасть с ошибкой в консоли, и встаёт всё: сценки,
    дыхание, взгляд, отсчёт пауз. Робот при этом продолжает получать кадры
    застывшей страницы и считает, что всё хорошо.
    """
    section("ПК: персонаж живёт сам")
    import time as _t
    а = kuzya_pc.Аватар()
    а.принять({"эмоция": "спокоен", "человек": True, "взгляд": 0.0})
    а.пульс()
    имена = set()
    for _ in range(30):                  # полторы секунды тишины от страницы
        _t.sleep(0.05)
        сцена = (а._последнее or {}).get("сцена") or {}
        if сцена.get("имя"):
            имена.add(сцена["имя"])
    check("без единого запроса страницы персонаж всё равно играет",
          len(имена) >= 1, True)
    # И второй раз пульс не заводится: два потока двигали бы сценариста вдвое
    # быстрее, и все паузы в библиотеке стали бы вдвое короче.
    было = а._пульс
    а.пульс()
    check("пульс заводится один раз, а не по потоку на вызов", а._пульс is было, True)


def test_сценарист_переживает_пропуск_и_перезапуск() -> None:
    """Час без кадров и перезапуск службы — не повод выдумывать и забывать.

    Два разных случая с одинаковым симптомом «персонаж ведёт себя дико».
    Первый: экран занят картинкой с ПК, свой сценарист не нужен, и его не
    зовут; ПК пропадает — в первый же кадр он сравнивает нынешнее состояние
    с часовой давности и выдаёт пачку событий разом. Второй: автообновление
    перезапустило службу, и персонаж проснулся с чистой головой — второе «С
    добрым утром» за одно утро, повтор сценки минутной давности.
    """
    section("сценарист: пропуск времени и память между запусками")
    import random as _rnd
    import time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    покой = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    рядом = dict(покой, человек=True, взгляд=0.0)

    # --- пропуск: перематываем, а не выдумываем -----------------------------
    сц = sc.Сценарист(_rnd.Random(5))
    сц.кадр(покой, 0.0)
    for i in range(1, 60):
        сц.кадр(рядом, i * 0.1)
    отдыхи_до = dict(сц._играли)
    р = сц.кадр(dict(рядом, музыка={"играет": True}, эмоция="рад"), 3600.0)
    повод = sc.ПО_ИМЕНИ[р["имя"]].когда if р["имя"] else ""
    check("после часа без кадров персонаж не выдаёт пачку событий разом",
          повод in sc.ФОНЫ or повод == "", True)
    сдвиги = {round(сц._играли[и] - отдыхи_до[и], 1) for и in отдыхи_до}
    check("…и отдых сценок сдвинут вместе с часами, а не обнулён",
          len(сдвиги) == 1 and abs(сдвиги.pop() - 3594.0) < 1.0, True)
    check("…а жест, начатый в прошлой жизни, не доигрывается",
          сц._сцена is None or сц._начало >= 3600.0, True)

    # --- биография переживает перезапуск ------------------------------------
    сцу = sc.Сценарист(_rnd.Random(5))
    сцу.кадр(покой, 0.0)
    t = 0.1
    while t < 120.0:
        сцу.кадр(рядом, round(t, 3), час=9, день=12, месяц=9, день_недели=4)
        t += 0.1
    сейчас = _time.time()
    снимок = сцу.снимок(t, сейчас)
    check("в снимке есть и сыгранное, и сказанное сегодня",
          (len(снимок["играли"]) > 0, "утро_встреча" in снимок["было_сегодня"]),
          (True, True))
    # Новый процесс: своё время с нуля, настенные часы ушли на три часа вперёд.
    новый = sc.Сценарист(_rnd.Random(9))
    check("снимок поднимается в свежем сценаристе",
          новый.восстановить(снимок, 0.0, сейчас + 3 * 3600), True)
    check("«С добрым утром» после перезапуска второй раз не звучит",
          новый._расписание(рядом, 9, 12, 9, 4), [])
    check("…и отдых сыгранного не уехал в будущее (иначе вся библиотека немая)",
          all(к <= 0.0 for к in новый._играли.values()), True)
    # Битый файл не должен мешать персонажу ожить — это важнее самой памяти.
    #
    # ИСКЛЮЧЕНИЕ ЛОВИМ ЗДЕСЬ, А НЕ ДАЁМ ЕМУ УНЕСТИ ПРОГОН. Именно «падает
    # исключением» — худший исход этой поломки: служба лица уходит в цикл
    # перезапусков, экран гаснет и загорается по кругу, а понять почему можно
    # только через journalctl, и то зная, куда смотреть. Сторож обязан
    # НАЗВАТЬ такой исход, а не разделить его: мутационный прогон честно
    # сказал, что рухнувшая проверка ничего не доказывает.
    def поднять(что):
        try:
            return новый.восстановить(что, 0.0, сейчас)
        except Exception as беда:                        # noqa: BLE001
            return f"упало: {type(беда).__name__}"
    check("битая память не роняет и не мешает: молча живём на авторских весах",
          (поднять({"играли": "чушь", "повторов": None}), поднять(None)),
          (False, False))


def test_повод_без_свободной_сценки_не_молчит() -> None:
    """У бедного пула все сценки на отдыхе — и что, не отвечать человеку?

    Раньше именно так: повод приходил, свободной сценки не находилось, и
    персонаж молчал. Для пула из одной сценки это обычное дело — скажи
    «спасибо» дважды подряд, и на второе он не ответит вовсе. Повторить жест
    чуть раньше срока — мелочь, а не ответить — грубость.
    """
    section("сценарист: повод всегда чем-то отыгран")
    import random as _rnd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    сц = sc.Сценарист(_rnd.Random(4))
    пул = [с.имя for с in sc.СЦЕНКИ if с.когда == "услышал_люблю"]
    for имя in пул:                      # всех разом отправляем на отдых
        сц._играли[имя] = 1000.0
    выбор = сц._выбрать(["услышал_люблю"], 1000.1)
    check("все сценки повода на отдыхе — всё равно есть чем ответить",
          выбор is not None and выбор.имя in пул, True)
    check("…и берётся та, которую не показывали дольше всех",
          сц._выбрать(["услышал_люблю"], 1000.1).имя,
          min(пул, key=lambda и: сц._играли.get(и, float("-inf"))))
    check("а повода, которого нет в библиотеке, выдумывать не из чего",
          сц._выбрать(["такого_повода_нет"], 1000.1), None)


def test_три_поздравления_не_сгорают_разом() -> None:
    """Праздничная пятница: поздравлений три, а кадр берёт одно.

    Метка «сказано сегодня» ставилась в момент ВЫБОРА, а не показа: два
    других поздравления помечались сказанными, хотя их никто не видел. В
    лучшем случае человек замечал мелькнувшее «Понедельник…» вместо «С Новым
    годом!» — и до следующего года.
    """
    section("сценарист: поздравления не сгорают")
    import random as _rnd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    сц = sc.Сценарист(_rnd.Random(6), праздники={"12-9": "С праздником!"})
    рядом = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
             "музыка": {"играет": False}}
    сц.кадр(dict(рядом, человек=False), 0.0)
    сыграны, t = [], 0.1
    while t < 90.0:
        р = сц.кадр(рядом, round(t, 3), час=9, день=12, месяц=9, день_недели=4)
        if р["имя"]:
            повод = sc.ПО_ИМЕНИ[р["имя"]].когда
            if повод in sc.РАСПИСАНИЕ and повод not in сыграны:
                сыграны.append(повод)
        t += 0.1
    check("в праздничное утро пятницы звучит не одно поздравление из трёх",
          len(сыграны) >= 2, True)
    check("…и каждое отмечено сказанным только после показа",
          sorted(сц._было_сегодня) == sorted(сыграны), True)


def test_настроение() -> None:
    """Настроение как величина, а не ярлык: копится, остывает, помнит себя."""
    section("настроение: пять осей и долг сна")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import mood

    # ШУМ НЕ ПРОДЛЕВАЕТ ЭПИЗОД. Толчки приходят каждый кадр и почти все
    # крошечные: человек шевельнулся, пеленг дрогнул. Если бы часы затухания
    # сбрасывал любой из них, персонаж не остыл бы НИКОГДА — вечно «только
    # что обрадовался». Поэтому часы сбрасывает лишь заметный толчок.
    def радость_после(шум):
        н = mood.Настроение()
        н.остыть(0.0)
        н.добавить("радость", 0.8, 0.0)
        t = 0.0
        for i in range(100):
            t += 1.0
            if шум:
                н.добавить("радость", 0.03 if i % 2 else -0.03, t)
            н.остыть(t)
        return н.значение("радость")
    без_шума, с_шумом = радость_после(False), радость_после(True)
    check("сто мелких толчков подряд остывают ровно так же, как тишина",
          abs(без_шума - с_шумом) < 0.05 and без_шума < 0.5, True)
    # А один заметный толчок часы сбрасывает — иначе всплеск не всплеск.
    н1 = mood.Настроение()
    н1.остыть(0.0)
    н1.добавить("радость", 0.8, 0.0)
    for сек in range(1, 101):
        н1.остыть(float(сек))
        if сек == 50:
            н1.добавить("радость", 0.4, float(сек))
    check("…а заметный толчок посреди спада продлевает эпизод",
          н1.значение("радость") > без_шума, True)

    # ВОЗБУЖДЕНИЕ — УРОВЕНЬ, А НЕ ВСПЫШКА: скорость зависит от высоты.
    def возбуждение_через(толчок, секунд):
        н = mood.Настроение()
        н.остыть(0.0)
        н.добавить("возбуждение", толчок, 0.0)
        for сек in range(1, int(секунд) + 1):
            н.остыть(float(сек))
        return н.значение("возбуждение")
    check("рабочая середина держится: через две минуты всё ещё заметна",
          возбуждение_через(0.2, 120) > 0.2, True)     # из 0.3 в 0.5
    check("с пика уходит за минуту: держать пик дорого",
          возбуждение_через(0.65, 60) < 0.8, True)     # из 0.3 в 0.95
    check("а с низа — за секунды: низкое возбуждение неустойчиво",
          возбуждение_через(-0.25, 15), 0.0)           # из 0.3 в 0.05

    # ЭПИЗОД НЕ ЗАВИСИТ ОТ ЧАСТОТЫ КАДРОВ. Закон эпизода — «сколько прошло с
    # толчка», а не «сколько раз мы его применили». Первая версия множила
    # значение на долю КАЖДЫЙ кадр, и на тридцати кадрах в секунду радость
    # умирала мгновенно вместо минуты.
    def радость_через(секунд, шаг):
        н = mood.Настроение()
        н.остыть(0.0)
        н.добавить("радость", 0.8, 0.0)
        сек = 0.0
        while сек < секунд:
            сек += шаг
            н.остыть(сек)
        return н.значение("радость")
    редко, часто = радость_через(100.0, 1.0), радость_через(100.0, 1.0 / 30.0)
    check("эпизод живёт по часам, а не по кадрам: 1 к/с и 30 к/с дают одно",
          abs(редко - часто) < 0.03 and редко > 0.1, True)

    # ДОВЕРИЕ НЕ ТАЕТ ОТ ВРЕМЕНИ. Его можно только потерять поступком.
    н4 = mood.Настроение()
    н4.остыть(0.0)
    н4.добавить("доверие", 0.4, 0.0)
    было = н4.значение("доверие")
    for час in range(1, 25):
        н4.остыть(час * 3600.0)
    check("доверие не тает за сутки простоя", abs(н4.значение("доверие") - было) < 0.01, True)
    н4.добавить("доверие", -1.0, 86400.0)
    check("…но теряется одним рывком", н4.значение("доверие"), 0.0)

    # ЯРЛЫК «ТИХО» ПЕРВЫМ — предохранитель от навязчивости.
    н5 = mood.Настроение()
    н5.остыть(0.0)
    н5.добавить("радость", 0.9, 0.0)
    н5.добавить("возбуждение", -0.2, 0.0)    # из 0.3 в 0.1
    check("радость при низком возбуждении — «тихо», а не «живо»", н5.ярлык(), "тихо")

    # ДОЛГ СНА И ГИСТЕРЕЗИС.
    н6 = mood.Настроение()
    н6.остыть(0.0)
    for час in range(1, 25):
        н6.прожить_сон(False, час * 3600.0)
        н6.остыть(час * 3600.0)
    check("сутки бодрствования копят долг сна", н6.долг_сна > mood.ЗАСЫПАТЬ_ПРИ, True)
    check("и клонит в сон", н6.клонит_в_сон(24 * 3600.0), True)
    # ГИСТЕРЕЗИС ПРОВЕРЯЕТСЯ ТОЛЬКО В ПОЛОСЕ МЕЖДУ ПОРОГАМИ. Долг, упавший
    # ниже порога засыпания, но ещё не ниже порога пробуждения, — ровно то
    # место, где существо без гистерезиса начинает дёргаться: засыпает и
    # просыпается каждые несколько секунд. Это худшее, что можно показать
    # человеку: не «живой», а «сломанный».
    т = 24 * 3600.0
    while н6.долг_сна > (mood.ЗАСЫПАТЬ_ПРИ + mood.ПРОСЫПАТЬСЯ_ПРИ) / 2 and т < 48 * 3600.0:
        т += 60.0
        н6.прожить_сон(True, т)
        н6.остыть(т)
    в_полосе = mood.ПРОСЫПАТЬСЯ_ПРИ < н6.долг_сна < mood.ЗАСЫПАТЬ_ПРИ
    check("долг упал в полосу между порогами — и там он ещё спит, а не дёргается",
          (в_полосе, н6.клонит_в_сон(т)), (True, True))
    while н6.долг_сна > 0.0 and т < 72 * 3600.0:  # досыпает до нуля
        т += 60.0
        н6.прожить_сон(True, т)
        н6.остыть(т)
    check("выспался — просыпается", н6.клонит_в_сон(т), False)

    # БОДРОСТЬ: суточная кривая. Три часа дня — не то же, что десять утра.
    н7 = mood.Настроение()
    check("в тонусе к десяти утра, провал к трём дня, ночью низко",
          (н7.бодрость(10) > н7.бодрость(15), н7.бодрость(15) > н7.бодрость(3)),
          (True, True))

    # ПАМЯТЬ О ПРОШЛОМ: реакции на производную.
    н8 = mood.Настроение()
    н8.остыть(0.0)
    н8.добавить("радость", 0.8, 0.0)
    for сек in range(1, 11):
        н8.остыть(float(сек))
    check("помнит, каким был десять секунд назад", н8.было(9.0, "радость") is not None, True)
    check("а что было час назад — не помнит и не выдумывает",
          н8.было(3600.0, "радость"), None)

    # ФАЙЛ. Битое поле — не повод падать: лицо обязано подняться всегда.
    н9 = mood.Настроение()
    н9.восстановить({"радость": 0.5, "доверие": "ерунда", "долг_сна": float("nan")})
    check("из файла берём здоровое, кривое пропускаем",
          (н9.значение("радость"), н9.значение("доверие") > 0.0, н9.долг_сна),
          (0.5, True, 0.0))
    check("снимок — только числа", all(isinstance(з, (int, float))
                                       for з in н9.снимок().values()), True)


def test_опыт() -> None:
    """Обучение: заходит — чаще, не заходит — реже, но репертуар цел."""
    section("опыт: чему персонаж учится у этого дома")
    import random as _rnd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import learn

    # ПОТОЛОК И ПОЛ. Обучение УМНОЖАЕТ авторский вес, а не заменяет его.
    # Без потолка одна зашедшая сценка выедает весь репертуар: персонаж
    # находит любимый трюк и делает только его.
    о = learn.Опыт(_rnd.Random(1))
    for i in range(50):
        о.наблюдать("хорошая", "покой", 1.0, 1.0, i * 60.0)
    тяги = [о.множитель("хорошая", "покой", 1.0, 3000.0) for _ in range(3000)]
    check("пятьдесят «зашло» поднимают множитель, но не выше потолка",
          (sum(тяги) / len(тяги) > 1.5, max(тяги) <= learn.МНОЖИТЕЛЬ_ВЕРХ + 1e-9),
          (True, True))
    check("…и потолок не декоративный: в него упираются",
          sum(1 for м in тяги if м >= learn.МНОЖИТЕЛЬ_ВЕРХ - 1e-9) > len(тяги) // 10, True)
    о2 = learn.Опыт(_rnd.Random(2))
    for i in range(50):
        о2.наблюдать("плохая", "покой", 1.0, 0.0, i * 60.0)
    тяги2 = [о2.множитель("плохая", "покой", 1.0, 3000.0) for _ in range(3000)]
    сред2 = sum(тяги2) / len(тяги2)
    check("пятьдесят «не зашло» опускают множитель, но не ниже пола",
          (сред2 < 0.7, min(тяги2) >= learn.МНОЖИТЕЛЬ_НИЗ - 1e-9), (True, True))
    # И не приклеиваются к полу намертво: у разлюбленной сценки остаётся
    # разброс, а значит остаётся и шанс показать себя снова.
    check("…но и не приклеиваются к полу: разброс у разлюбленной сохраняется",
          max(тяги2) > learn.МНОЖИТЕЛЬ_НИЗ + 0.05, True)

    # СИЛА АПРИОРА: три наблюдения не переворачивают картину.
    о3 = learn.Опыт(_rnd.Random(3))
    for i in range(3):
        о3.наблюдать("новая", "покой", 1.0, 1.0, i * 60.0)
    # Судим по СРЕДНЕМУ: отдельная тяга — это Thompson sampling, его разброс
    # и есть тот самый «а вдруг», ради которого малоизученная сценка иногда
    # выигрывает. Смещение же ожидания после трёх наблюдений должно быть мало.
    среднее = sum(о3.множитель("новая", "покой", 1.0, 200.0)
                  for _ in range(2000)) / 2000.0
    check("три наблюдения не сдвигают ожидаемый множитель больше чем на 15%",
          abs(среднее - 1.0) <= 0.15, True)

    # ε-ПОЛ. Вкус меняется, а статистика, которая никогда не проверяет
    # отвергнутое, об этом не узнает.
    о4 = learn.Опыт(_rnd.Random(4))
    мимо = sum(1 for _ in range(1000) if о4.мимо_обучения())
    check("каждый десятый выбор идёт мимо обучения, по авторским весам",
          0.07 <= мимо / 1000.0 <= 0.14, True)

    # ЗАБЫВАНИЕ. Тает ПРЕВЫШЕНИЕ НАД АПРИОРОМ, а не сама статистика: гасить
    # α и β одним множителем бесполезно — θ = α/(α+β) от этого не меняется, и
    # «забывание» сохраняет ровно тот вывод, который персонаж сделал в первый
    # месяц.
    о5 = learn.Опыт(_rnd.Random(5))
    for i in range(50):
        о5.наблюдать("забытая", "покой", 1.0, 1.0, i * 60.0)
    def через(суток):
        когда = суток * 24 * 3600.0
        return sum(о5.множитель("забытая", "покой", 1.0, когда)
                   for _ in range(3000)) / 3000.0
    свежо, месяц, год = через(0.05), через(42), через(365)
    check("забывает по расписанию: через полтора месяца ближе к единице, через год — единица",
          (свежо > 1.2, месяц < свежо, abs(год - 1.0) < 0.05), (True, True, True))

    # ПРАВО ПЕРЕДУМАТЬ. Пятьсот удачных наблюдений не должны делать сценку
    # неопровержимой: существо, которое не может передумать, не учится, а
    # окостеневает. Потолок наблюдений держит старое в узде.
    о5б = learn.Опыт(_rnd.Random(55))
    for i in range(500):
        о5б.наблюдать("вкус", "покой", 1.0, 1.0, i * 60.0)
    т = 500 * 60.0
    полюбил = sum(о5б.множитель("вкус", "покой", 1.0, т) for _ in range(2000)) / 2000.0
    for i in range(60):
        о5б.наблюдать("вкус", "покой", 1.0, 0.0, т + i * 60.0)
    т2 = т + 60 * 60.0
    разлюбил = sum(о5б.множитель("вкус", "покой", 1.0, т2) for _ in range(2000)) / 2000.0
    check("пятьсот «зашло» не делают сценку вечной: шестьдесят «не зашло» переубеждают",
          (полюбил > 1.2, разлюбил < 1.0), (True, True))

    # НЕИЗВЕСТНАЯ СЦЕНКА НИЧЕГО НЕ МЕНЯЕТ: без опыта жребий идёт как раньше.
    check("про что не знаем — того не трогаем",
          learn.Опыт(_rnd.Random(6)).множитель("никогда", "покой", 1.0, 0.0), 1.0)

    # БАЗОВАЯ ЛИНИЯ КОНТЕКСТА. Учимся на превышении над обычным для этого
    # места и часа: ночью отклик ниже У ВСЕГО, и без линии персонаж выучил бы
    # «ночью всё плохо» вместо «ночью вот это лучше вот того».
    о6 = learn.Опыт(_rnd.Random(7))
    ночь = ("покой_ночью", "ночь")
    for i in range(30):                       # ночью всё вяло: отклик 0.2
        о6.наблюдать("ночная", "покой_ночью", 1.0, 0.2, i * 60.0, контекст=ночь)
    ночной = sum(о6.множитель("ночная", "покой_ночью", 1.0, 2000.0)
                 for _ in range(100)) / 100.0
    check("вялый отклик, обычный для этого контекста, не наказывает сценку",
          ночной > 0.9, True)

    # РАСПОРЯДОК. Первые двое суток — молчим.
    о7 = learn.Опыт(_rnd.Random(8))
    о7.отметить_час(19, False, 0.9, 0.5)
    check("по одному дню не гадаем: предсказание молчит", о7.ждать_человека(19, False), None)
    for сутки in range(1, 15):
        for час in range(24):
            о7.отметить_час(час, False, 0.9 if час == 19 else 0.05, float(сутки))
    вечером = о7.ждать_человека(19, False)
    днём = о7.ждать_человека(11, False)
    check("через две недели знает, что в семь вечера он дома",
          (вечером is not None and вечером > 0.5, днём is not None and днём < 0.4),
          (True, True))

    # СВОДКА — для лога и режиссёра, а не для человека на экране.
    о8 = learn.Опыт(_rnd.Random(9))
    for i in range(20):
        о8.наблюдать("заходит", "покой", 1.0, 1.0, i * 60.0)
    строки = о8.сводка(5, t=1200.0)
    check("сводка называет сценку и число наблюдений",
          bool(строки) and "заходит" in строки[0], True)

    # ФАЙЛ. Дальность хранится как «сколько секунд назад»: монотонные часы
    # после перезагрузки начинаются заново, и абсолютная отметка из прошлой
    # жизни означала бы что угодно.
    о9 = learn.Опыт(_rnd.Random(10))
    for i in range(20):
        о9.наблюдать("своя", "покой", 1.0, 1.0, i * 60.0)
    сн = о9.снимок(t=1200.0)
    о10 = learn.Опыт(_rnd.Random(10))
    о10.восстановить(сн, t=0.0)
    check("после перезапуска опыт тот же, а не начатый заново",
          abs(sum(о10.множитель("своя", "покой", 1.0, 0.0) for _ in range(200)) / 200.0
              - sum(о9.множитель("своя", "покой", 1.0, 1200.0) for _ in range(200)) / 200.0)
          < 0.1, True)
    о11 = learn.Опыт(_rnd.Random(11))
    о11.восстановить({"сценки": {"кривая": ["ерунда", None, 5]},
                      "распорядок": {"плохой ключ": 0.5},
                      "суток": float("inf")}, t=0.0)
    check("битый файл не мешает подняться: берём здоровое, кривое пропускаем",
          (о11.множитель("кривая", "покой", 1.0, 0.0), о11.ждать_человека(19, False)),
          (1.0, None))


def test_обучение_на_поведении() -> None:
    """Персонаж учится у ЭТОГО дома — но не сжимает репертуар и не косте­неет.

    Здесь проверяется не арифметика (она в test_опыт), а петля целиком:
    сыграл — посмотрел, что человек ответил, — запомнил — стал выбирать иначе.
    Судья — виртуальный хозяин с вкусом: на сценки с руками он отзывается, на
    прочие нет. Через две недели домовёнок обязан это заметить.
    """
    section("обучение: две недели с хозяином, у которого есть вкус")
    import random as _rnd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    def с_руками(имя):
        сц = sc.ПО_ИМЕНИ[имя]
        return any(sc.РУКА_Л in п or sc.РУКА_П in п for _, п in сц.кадры)

    def прожить(суток, любит_руки=True, шаг=0.5):
        сцн = sc.Сценарист(_rnd.Random(21))
        сыграно, t = [], 0.0
        нравилось_до = 0.0
        всего = суток * 24 * 3600.0
        while t < всего:
            час = int((t / 3600.0) % 24.0)
            # Хозяин дома вечером; отклик — это его поведение, а не оценка:
            # понравилось — задерживается и заговаривает, нет — отворачивается.
            дома = 18 <= час < 23
            нравится = t <= нравилось_до
            # «Говорит» — это РОБОТ говорит, а не человек; человек слышен
            # через «услышал». Перепутать легко, а цена высокая: пока робот
            # говорит, обучение выключено вовсе, и хозяин, «говорящий» этим
            # полем, глушил бы ровно те наблюдения, ради которых он тут.
            с = {"эмоция": "спокоен", "человек": дома,
                 "взгляд": 0.0 if (дома and нравится) else (0.95 if дома else None),
                 "услышал": "ага" if нравится else "",
                 "музыка": {"играет": False}, "ночь": час >= 23 or час < 7}
            к = сцн.кадр(с, t, час=час, минута=int((t / 60.0) % 60))
            имя = к.get("имя") or ""
            # ЧЕЛОВЕК ОТЗЫВАЕТСЯ НА ТО, ЧТО ВИДИТ ПЕРЕД СОБОЙ, — то есть на
            # сценку открытого окна, а не на любую сыгранную. Разница не
            # педантизм: без неё сценка, сыгранная через секунду после
            # понравившейся, забирает себе чужую заслугу, и вместо вкуса
            # персонаж учит шум. С этой поправкой θ любимых поднялась с 0.60
            # до 0.81, а нелюбимых упала с 0.44 до 0.22.
            окно = сцн._окно
            нравилось_до = (t + шаг if окно and с_руками(окно["имя"]) == любит_руки
                            else нравилось_до)
            if имя and дома and (not сыграно or сыграно[-1] != имя):
                сыграно.append(имя)
            t += шаг
        return сцн, сыграно

    сцн, сыграно = прожить(14)
    первый_день = сыграно[:len(сыграно) // 14]
    последний = сыграно[-len(сыграно) // 14:]
    def доля_рук(ряд):
        фоновые = [и for и in ряд if sc.ПО_ИМЕНИ[и].когда in sc.ФОНЫ]
        return (sum(1 for и in фоновые if с_руками(и)) / len(фоновые)) if фоновые else 0.0
    было, стало = доля_рук(первый_день), доля_рук(последний)
    check("за две недели доля того, на что дом отзывается, выросла",
          стало > было + 0.05, True)
    # ОДНОЙ СТАТИСТИКИ МАЛО — НУЖНО, ЧТОБЫ ОНА ДОХОДИЛА ДО ПОВЕДЕНИЯ. Это
    # разные вещи, и в первой версии они разошлись: персонаж выучил вкус
    # хозяина отлично (θ 0.81 против 0.22) и не поменял поведения НИ НА
    # СКОЛЬКО, потому что выбор к тому времени определялся одной давностью
    # показа — привыкание без потолка давало множители порядка 1e-11, рядом с
    # которыми обучение просто не существовало.
    def средний_множитель(с_руками_ли):
        свои = [и for и in сцн.опыт._стат if с_руками(и) == с_руками_ли]
        if not свои:
            return 0.0
        когда = 14 * 24 * 3600.0
        return sum(сцн.опыт.множитель(и, sc.ПО_ИМЕНИ[и].когда, sc.ПО_ИМЕНИ[и].вес, когда)
                   for и in свои) / len(свои)
    check("и статистика отделила любимое от нелюбимого, а не только поведение",
          средний_множитель(True) > средний_множитель(False) * 1.5, True)

    # РЕПЕРТУАР НЕ СЖИМАЕТСЯ. Это вторая половина условия и по-настоящему
    # трудная: выучить любимое легко, трудно не выкинуть остальное. Персонаж,
    # который нашёл удачный трюк и делает только его, не «выучился» — он
    # сломался, и человек это увидит быстрее любой метрики.
    последних = set(последний)
    check("…но репертуар не схлопнулся: разного за последние сутки всё ещё много",
          len(последних) >= 40, True)
    нелюбимых = [и for и in последних if not с_руками(и)]
    check("…и нелюбимое не исчезло вовсе — просто стало реже",
          len(нелюбимых) >= 10, True)

    # СОБЫТИЙНЫЕ СЦЕНКИ НЕ УЧАТСЯ НИКОГДА. Приветствие, тревога, батарея,
    # таймер играются не потому, что персонаж их выбрал, а потому, что
    # случилось. «Выучить» на них можно только одно: реже здороваться и реже
    # предупреждать о севшей батарее.
    событийные = [и for и in сцн.опыт._стат if sc.ПО_ИМЕНИ[и].когда not in sc.ФОНЫ]
    check("ни одна событийная сценка не получила ни одного наблюдения",
          событийные, [])

    # В ПУСТОЙ КОМНАТЕ НЕ УЧИМСЯ. Иначе через месяц персонаж выучит ровно
    # одно: «ночью и в пустой комнате всё плохо» — что правда про комнату, а
    # не про сценки.
    сцп = sc.Сценарист(_rnd.Random(22))
    пусто = {"эмоция": "спокоен", "человек": False, "взгляд": None,
             "музыка": {"играет": False}}
    for i in range(4000):
        сцп.кадр(пусто, i * 0.5, час=14)
    check("сценка, сыгранная в пустой комнате, не меняет ни альфу, ни бету",
          сцп.опыт._стат, {})

    # ОДИН ЭПИЗОД — ОДНО НАБЛЮДЕНИЕ, а не тридцать в секунду: иначе длинная
    # сценка перевесила бы короткую просто потому, что дольше висела на
    # экране, и обучение выучило бы длительность вместо удачности.
    сцо = sc.Сценарист(_rnd.Random(23))
    рядом = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
             "музыка": {"играет": False}}
    for i in range(1200):
        сцо.кадр(рядом, i * 0.1, час=14)
    наблюдений = sum(max(0.0, a + b - 2 * __import__("learn").СИЛА_АПРИОРА)
                     for a, b, _, _ in сцо.опыт._стат.values())
    сыграно_фоновых = sum(1 for и in сцо._играли if sc.ПО_ИМЕНИ[и].когда in sc.ФОНЫ)
    check("наблюдений не больше, чем сыгранных фоновых сценок",
          наблюдений <= сыграно_фоновых + 1, True)

    # ОПЫТ ПЕРЕЖИВАЕТ ПЕРЕЗАПУСК. Иначе всё выученное сгорает при каждом
    # автообновлении — а служба лица перезапускается по нескольку раз в день.
    снимок = сцн.снимок(t=14 * 24 * 3600.0, сейчас=1_700_000_000.0)
    новый = sc.Сценарист(_rnd.Random(21))
    новый.восстановить(снимок, t=0.0, сейчас=1_700_000_000.0)
    check("выученное переживает перезапуск службы",
          len(новый.опыт._стат) == len(сцн.опыт._стат) and len(новый.опыт._стат) > 20,
          True)


def test_настроение_доходит_до_поведения() -> None:
    """Настроение обязано быть ВИДНО, иначе это просто ещё один счётчик.

    Пять осей в mood.py можно сколько угодно копить и остужать — если они не
    меняют ни одного кадра, персонаж от этого не оживёт. Здесь проверяется не
    арифметика (она в test_настроение), а то, что величина доходит до дела:
    темп сценок и срок, через который персонаж начинает звать человека.
    """
    section("настроение: видно ли его снаружи")
    import random as _rnd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    пусто = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    рядом = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
             "музыка": {"играет": False}}

    # ДЕНЬ В ПУСТОЙ КОМНАТЕ ПРИСМИРЯЕТ. Общительность — это УРОВЕНЬ, а не
    # вспышка, и выяснилось это замером: пока она была эпизодом, закон
    # возврата к нулю побеждал каплю одиночества, и за час она приходила
    # ровно к нулю. Величина была, эффекта не было.
    сцо = sc.Сценарист(_rnd.Random(51))
    for i in range(int(3600 * 10)):
        сцо.кадр(пусто, i / 10, час=14)
    одинокая = сцо.настроение.значение("общительность")
    check("час в пустой комнате и правда присмиряет, а не оставляет ноль",
          одинокая < -0.2, True)
    # И НЕ ЗАВИСИТ ОТ ЧАСТОТЫ КАДРОВ. Всё, что меряется «в час» и «в минуту»,
    # обязано считаться по ЧАСАМ: первая версия отнимала по чуть-чуть на
    # кадр, и на тридцати кадрах в секунду общительность падала в пол за
    # полминуты — то есть величина зависела от того, как быстро работает
    # железо, а не от того, что происходит с персонажем.
    def одиночество_при(шаг):
        сц = sc.Сценарист(_rnd.Random(51))
        t = 0.0
        while t < 3600.0:
            t += шаг
            сц.кадр(пусто, t, час=14)
        return сц.настроение.значение("общительность")
    редко, часто = одиночество_при(1.0), одиночество_при(1.0 / 30.0)
    check("одиночество копится по часам, а не по кадрам: 1 к/с и 30 к/с дают одно",
          abs(редко - часто) < 0.05, True)

    # ВСТРЕЧА ОЖИВЛЯЕТ. Один толчок не отменяет дня одиночества целиком — но
    # заметно сдвигает: так и читается «обрадовался, но ещё не отошёл».
    for i in range(300):
        сцо.кадр(рядом, 3600.0 + i / 10, час=14)
    check("человек пришёл — общительность поднялась, но не в ноль разом",
          одинокая < сцо.настроение.значение("общительность") < 0.0, True)

    # ТЕМП. Растормошённый частит, притихший тянет. Проверяем ровно то, что
    # видно снаружи: длину пауз между сценками.
    def средняя_пауза(возбуждение):
        сц = sc.Сценарист(_rnd.Random(52))
        сц.кадр(пусто, 0.0, час=14)
        сц.настроение.остыть(0.0)
        сц.настроение.добавить("возбуждение", возбуждение - 0.3, 0.0)
        начала, прошлое = [], ""
        for i in range(1, 12000):
            к = сц.кадр(пусто, i / 10, час=14)
            имя = к.get("имя") or ""
            if имя and имя != прошлое:
                начала.append(i / 10)
            прошлое = имя
            # Держим возбуждение на месте: меряем темп, а не остывание.
            сц.настроение._знач["возбуждение"] = возбуждение
        разрывы = [б - а for а, б in zip(начала, начала[1:])]
        return sum(разрывы) / len(разрывы) if разрывы else 0.0
    тихо, живо = средняя_пауза(0.05), средняя_пауза(0.95)
    check("растормошённый частит, притихший тянет — и это видно в паузах",
          живо < тихо * 0.9, True)

    # ЗОВ. Присмиревший не лезет сразу — сначала приглядывается. «Лезет /
    # не лезет» человек замечает мгновенно, поэтому это важнее темпа.
    def через_сколько_зовёт(общительность):
        сц = sc.Сценарист(_rnd.Random(53))
        сц.кадр(пусто, 0.0, час=14)
        сц.настроение.остыть(0.0)
        for i in range(1, 400):
            t = i / 10
            сц.настроение._знач["общительность"] = общительность
            сц.кадр(рядом, t, час=14)
            if сц._зовёт:
                return t
        return None
    робкий, смелый = через_сколько_зовёт(-0.5), через_сколько_зовёт(0.5)
    check("присмиревший начинает звать позже смелого",
          робкий is not None and смелый is not None and робкий > смелый, True)

    # И ВСЁ ЭТО ПЕРЕЖИВАЕТ ПЕРЕЗАПУСК. Доверие копится месяцами; начинать его
    # заново после каждого автообновления — значит не копить вовсе.
    сцп = sc.Сценарист(_rnd.Random(54))
    сцп.кадр(рядом, 0.0, час=14)
    сцп.настроение.добавить("доверие", 0.4, 0.0)
    снимок = сцп.снимок(t=100.0, сейчас=1_700_000_000.0)
    новый = sc.Сценарист(_rnd.Random(54))
    новый.восстановить(снимок, t=0.0, сейчас=1_700_000_000.0)
    check("настроение переживает перезапуск службы",
          abs(новый.настроение.значение("доверие")
              - сцп.настроение.значение("доверие")) < 0.01, True)


def test_ждёт_человека_к_его_часу() -> None:
    """Самое видимое, что умеет самообучение: он начинает ждать.

    Распорядок копится сам — двадцать четыре часа × будни/выходные, доля
    времени, когда человек был рядом. Через несколько суток персонаж знает,
    что к вечеру хозяин приходит, и к этому часу начинает поглядывать на
    дверь. Никакой модели: накопленная статистика, а выглядит как ожидание.
    """
    section("самообучение: ждёт человека к его часу")
    import random as _rnd
    from collections import Counter as _Счёт
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "face"))
    import scenes as sc

    сцн = sc.Сценарист(_rnd.Random(61))
    ждал, t = [], 0.0
    while t < 12 * 24 * 3600.0:
        час = int((t / 3600.0) % 24.0)
        сутки = int(t // 86400)
        дома = 18 <= час < 23
        с = {"эмоция": "спокоен", "человек": дома,
             "взгляд": 0.0 if дома else None, "музыка": {"играет": False},
             "ночь": час >= 23 or час < 7}
        к = сцн.кадр(с, t, час=час, минута=int((t / 60.0) % 60), день=1, месяц=9,
                     день_недели=сутки % 7)
        имя = к.get("имя") or ""
        if имя and sc.ПО_ИМЕНИ[имя].когда == "ждёт_человека":
            ждал.append((сутки, час))
        t += 1.0
    check("персонаж и правда ждёт, а не только умеет", bool(ждал), True)
    часы = sorted(_Счёт(ч for _, ч in ждал))
    # ЖДЁТ ВПЕРЁД, А НЕ В ТОТ ЖЕ ЧАС. Первая версия спрашивала распорядок про
    # ТЕКУЩИЙ час — и выходила бессмыслица: в час, когда хозяин обычно дома,
    # он дома, ждать некого; а в предыдущий час вероятность ещё низкая, и
    # персонаж не ждал вовсе. Сценок ожидания за двадцать суток было ноль.
    check("ждёт в час ПЕРЕД приходом, а не когда человек уже пришёл",
          часы, [17])
    # ПЕРВЫЕ СУТКИ МОЛЧИТ. Предсказывать по одному дню — это гадать, а
    # персонаж, угадавший один раз из трёх, выглядит хуже того, кто не
    # угадывает вовсе: первый кажется сломанным, второй — просто спокойным.
    # ЗАПАС В min() — НЕ ПЕДАНТИЗМ. Без него проверка не ПАДАЛА, а РУХАЛА с
    # «min() arg is an empty sequence», и мутационный прогон честно сказал:
    # падение не доказывает, что сторож есть. Сторож обязан отвечать числом
    # даже тогда, когда персонаж не сделал ничего, — иначе он не сторож, а
    # источник трассы.
    check("в первые сутки не гадает",
          min((д for д, _ in ждал), default=-1) >= 2, True)
    # И НЕ ЖДЁТ ВЕСЬ ДЕНЬ: ждущий всегда не ждёт никого.
    check("ждёт один час в сутки, а не круглые сутки", len(часы), 1)
    # ОЖИДАНИЕ — НЕ ЕДИНСТВЕННОЕ, ЧТО ОН ДЕЛАЕТ В ЭТОТ ЧАС. Человек читает
    # «ждёт» по тому, что персонаж ПЕРИОДИЧЕСКИ косится на дверь между
    # обычными делами. Персонаж, который целый час только смотрит на дверь,
    # читается не как ждущий, а как зависший.
    сц2 = sc.Сценарист(_rnd.Random(62))
    сц2.опыт._распорядок = {(False, ч): (0.95 if ч == 19 else 0.02) for ч in range(24)}
    сц2.опыт._суток_прожито = 9.0
    пусто = {"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}}
    поводы, прошлое = [], ""
    for i in range(3600 * 5):
        к = сц2.кадр(пусто, i / 5, час=18, минута=30, день=1, месяц=9, день_недели=0)
        имя = к.get("имя") or ""
        if имя and имя != прошлое:
            поводы.append(sc.ПО_ИМЕНИ[имя].когда)
        прошлое = имя
    ждущих = поводы.count("ждёт_человека")
    check("в час ожидания он и ждёт, и живёт своим — а не смотрит в дверь час подряд",
          0.15 < ждущих / max(1, len(поводы)) < 0.75, True)
    check("у повода «ждёт» хватает сценок, чтобы час не был одним жестом",
          len({и for и in sc.ПО_ИМЕНИ if sc.ПО_ИМЕНИ[и].когда == "ждёт_человека"}) >= 6,
          True)


def test_шесть_поломок_персонаж_жив() -> None:
    """Живость в тепличных условиях ничего не стоит.

    Робот стоит в квартире, где детектор тел может не подняться, компьютер —
    выключиться, часы — уехать (на плате нет батарейки), файл памяти —
    побиться, а служба лица — перезапуститься посреди сценки. Требование ко
    всем шести поломкам одно: персонаж ПРОДОЛЖАЕТ ЖИТЬ. Не «работает без
    ошибок» — живёт: картинка меняется, сценки идут, повторов нет.

    Случай №1 в аудите давал 2 из 10 МОЛЧА: детектор тел не поднимался
    пятнадцать месяцев из-за расхождения QoS, и половина библиотеки не
    играла ни разу — а в журнале не было ни строки.
    """
    section("надёжность: шесть поломок, и при каждой он жив")
    import importlib.util as _iu
    путь = Path(__file__).resolve().parent.parent / "scripts" / "прогон_жизни.py"
    спец = _iu.spec_from_file_location("прогон_жизни", путь)
    лин = _iu.module_from_spec(спец)
    спец.loader.exec_module(лин)

    check("поломок инсценируется шесть, а не сколько получилось",
          len(лин.ПОЛОМКИ), 6)
    норма = лин.прогнать(суток=0.25, зерно=9)
    мёртвые, вялые, рухнули = [], [], []

    def прожить(поломка):
        """Поломка НЕ ИМЕЕТ ПРАВА УРОНИТЬ ПРОГОН, и это часть проверки.

        Без этой обёртки сторож не падал, а РУХАЛ вместе с прогоном, и
        мутационная проверка честно сказала: падение не доказывает, что
        сторож есть. Исключение наружу — это и есть худший исход поломки
        (лицо уходит в цикл перезапусков), значит его надо ловить и
        НАЗЫВАТЬ, а не давать ему унести с собой весь разбор.
        """
        try:
            return лин.прогнать(суток=0.25, зерно=9, поломка=поломка)
        except Exception as беда:                        # noqa: BLE001
            рухнули.append(f"{поломка}: {type(беда).__name__}")
            return None

    for поломка in лин.ПОЛОМКИ:
        if поломка == "нет":
            continue
        м = прожить(поломка)
        if м is None:
            continue
        # ЗАМЕР, А НЕ ОБЕЩАНИЕ. «Неподвижность» — доля секунд, где поза не
        # менялась две секунды подряд; у живого нуль даже во сне, потому что
        # спящий дышит. «Повторов подряд» — одна и та же сценка дважды кряду.
        if м["неподвижность"] > 0.0 or м["повторов подряд"] > 0:
            мёртвые.append(поломка)
        # Сценок должно остаться заметно много. Слепой детектор честно
        # отнимает половину (половина библиотеки — про человека в кадре), и
        # это не поломка живости, а потеря повода; всё остальное обязано
        # держаться почти как обычно.
        порог = 0.4 if поломка == "детектор_молчит" else 0.8
        if м["_сценок всего"] < норма["_сценок всего"] * порог:
            вялые.append(f"{поломка}: {м['_сценок всего']} из {норма['_сценок всего']}")
    check("ни одна поломка не роняет прогон исключением", рухнули, [])
    check("при каждой поломке персонаж шевелится и не заедает", мёртвые, [])
    check("…и не впадает в спячку: сценки продолжают идти", вялые, [])

    # ОТДЕЛЬНО ПРО СЛЕПОЙ ДЕТЕКТОР. Тут важно не «сколько», а «почему»: без
    # человека в кадре половина сценок просто не имеет повода. Персонаж
    # обязан занять себя сам, а не замереть в ожидании.
    слепой = прожить("детектор_молчит") or {"_сценок всего": 0}
    check("слепой детектор — персонаж занимает себя сам, а не ждёт",
          слепой["_сценок всего"] > 100, True)

    # ЧАСЫ ВРУТ. У платы нет батарейки: до сверки по сети час и дата — это
    # враньё. Расписание обязано молчать (поздравить с понедельником в
    # неизвестный день хуже, чем промолчать), но жизнь не обязана.
    часы = прожить("часы_врут") or {"неподвижность": 1.0, "_сценок всего": 0}
    check("часы врут — расписание молчит, а персонаж живёт",
          (часы["неподвижность"], часы["_сценок всего"] > 100), (0.0, True))


def main() -> int:
    # Whisper в проверке не участвует: он про видеокарту, а не про логику.
    названные = (test_messages, test_stream, test_ping, test_whisper_fallback, test_tts, test_voiceprints, test_жилец_по_имени_что, test_warming, test_think_switch, test_model_choice,
                 test_tool_call, test_broken,
                 test_context_window, test_unthink, test_gigaam, test_no_initial_prompt, test_stt_confidence, test_health, test_avatar,
                 test_avatar_stream, test_avatar_reactions, test_scenes, test_director,
                 test_ollv_bridge,
                 test_дописанная_проверка_не_остаётся_незапущенной)
    # Кортеж выше — ручной и упорядоченный, добор — всё остальное, что похоже
    # на проверку. Сторож добора стоит в кортеже НАРОЧНО: добери его добором,
    # и сломанный добор молча унёс бы с собой единственного, кто это заметит.
    for test in названные + tuple(добор_проверок(названные)):
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
