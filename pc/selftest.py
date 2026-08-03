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

    def через(куски: list[str]) -> str:
        f = Unthink()
        out = "".join(f.feed(c) for c in куски)
        return out + f.close()

    check("закрывающий тег без открывающего",
          через(["Надо ответить коротко. ", "Пожалуй, так.", "</think>", "Привет!"]),
          "Привет!")
    check("обычная пара тегов",
          через(["<think>думаю</think>", "Готово."]), "Готово.")
    check("тег разорван между кусками",
          через(["думаю", "</thi", "nk>", "Готово."]), "Готово.")
    check("короткий ответ без размышлений доходит целиком",
          через(["Привет", ", Игорь!"]), "Привет, Игорь!")
    # Длинный ответ не должен застревать в буфере: после порога фильтр
    # перестаёт держать и пропускает поток дальше.
    длинный = "а" * 500
    check("длинный ответ не теряется", через([длинный]), длинный)


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
    for test in (test_messages, test_stream, test_tool_call, test_broken,
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
