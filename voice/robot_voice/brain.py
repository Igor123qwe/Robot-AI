"""Диалог с моделью: история, вызов инструментов, потоковый ответ.

Цикл «модель → инструмент → модель» написан руками, а не отдан tool_runner из
SDK. Причина в том, что робот может ходить не только к api.anthropic.com, но и
через сторонний роутер: tool_runner живёт на beta-эндпоинте, а прокси такое
обычно не пропускает. Обычный /v1/messages поддерживается везде.

Ответ отдаётся кусками, чтобы синтез речи начинался, не дожидаясь конца
генерации.
"""

from __future__ import annotations

import logging
from typing import Callable

import anthropic

from .config import SYSTEM_PROMPT, Config
from .tools import Tool

log = logging.getLogger(__name__)

# Сколько сообщений диалога держим в контексте (реплики плюс результаты
# инструментов). Для голосового робота длинная память не нужна: разговор идёт
# короткими репликами, а каждое лишнее сообщение оплачивается в каждом
# следующем запросе. Восемь — это примерно четыре обмена.
HISTORY_LIMIT = 8

# Предохранитель от зацикливания: модель не должна дёргать инструменты вечно.
MAX_TOOL_ROUNDS = 6

# Сколько ждём ответа. Умолчание SDK — десять минут: для голосового робота это
# вечность, потому что микрофон всё это время заглушён, а если модель успела
# вызвать drive — робот в это время едет и не слышит «стоп».
TIMEOUT_SECONDS = 25.0


class Brain:
    def __init__(self, cfg: Config, tools: list[Tool]) -> None:
        self.cfg = cfg
        self.tools = {t.name: t for t in tools}
        self.specs = [t.spec() for t in tools]

        client_args = {
            "api_key": cfg.api_key,
            "timeout": TIMEOUT_SECONDS,
            # Две попытки: сетевой сбой бывает, но ждать третью робот не может.
            "max_retries": 1,
        }
        if cfg.api_base:
            client_args["base_url"] = cfg.api_base
        self.client = anthropic.Anthropic(**client_args)

        self.history: list[dict] = []
        # Кэширование постоянной части промпта. Сторонний роутер может его не
        # знать — тогда отключим на первом же отказе и продолжим без него.
        self._use_cache = True
        # Расход с момента запуска — чтобы видеть цену вопроса, а не гадать.
        self.total_in = 0
        self.total_out = 0
        # Когда в последний раз говорили. Держим здесь, а не в цикле: цикл
        # перезапускается после сбоя, а разговор от этого не свежеет.
        self.last_talk = 0.0

    # --- параметры запроса ----------------------------------------------
    def _params(self, messages: list[dict]) -> dict:
        params = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.specs,
            messages=messages,
        )
        # effort понимают не все: у роутера или у сторонней модели его может
        # не быть. Пустое значение — не отправлять.
        if self.cfg.effort:
            params["output_config"] = {"effort": self.cfg.effort}

        if self._use_cache:
            # Промпт и схемы инструментов байт в байт одинаковы в каждом
            # запросе — около 1300 токенов, которые незачем оплачивать заново.
            # Отметка ставится на системный блок: инструменты идут перед ним,
            # поэтому кэшируются вместе с ним.
            params["system"] = [{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }]
        return params

    def _degrade(self, error: Exception) -> bool:
        """Отключает необязательный параметр, который не понял собеседник.

        Сторонний роутер может не знать ни кэширования, ни effort. Раньше
        отступление стояло вокруг вызова messages.stream(), а он запрос не
        делает — HTTP уходит только при входе в with. Поэтому отступление
        не срабатывало никогда, и робот отвечал «что-то пошло не так» на
        каждый разговорный вопрос до конца жизни процесса.
        """
        if self._use_cache:
            log.warning("кэширование промпта не принято (%s) — работаю без него", error)
            self._use_cache = False
            return True
        if self.cfg.effort:
            log.warning("параметр effort не принят (%s) — убираю", error)
            self.cfg.effort = ""
            return True
        return False

    def _round(self, messages: list[dict], on_text: Callable[[str], None]):
        """Один запрос к модели: отдаёт текст кусками, возвращает ответ целиком."""
        while True:
            try:
                with self.client.messages.stream(**self._params(messages)) as stream:
                    for event in stream:
                        if (event.type == "content_block_delta"
                                and getattr(event.delta, "type", "") == "text_delta"):
                            on_text(event.delta.text)
                    return stream.get_final_message()
            except anthropic.BadRequestError as e:
                if not self._degrade(e):
                    raise

    # --- один ход --------------------------------------------------------
    def reply(self, user_text: str, on_text: Callable[[str], None]) -> str:
        """Отвечает на реплику. on_text вызывается на каждый кусок текста."""
        messages = self.history + [{"role": "user", "content": user_text}]
        spoken: list[str] = []
        used_in = used_out = cached = written = 0
        truncated = False

        def collect(chunk: str) -> None:
            spoken.append(chunk)
            on_text(chunk)

        rounds = 0
        for rounds in range(1, MAX_TOOL_ROUNDS + 1):
            message = self._round(messages, collect)

            usage = getattr(message, "usage", None)
            if usage is not None:
                used_in += getattr(usage, "input_tokens", 0) or 0
                used_out += getattr(usage, "output_tokens", 0) or 0
                cached += getattr(usage, "cache_read_input_tokens", 0) or 0
                # Запись в кэш тоже оплачивается и в input_tokens не попадает:
                # без неё счётчик врал в меньшую сторону каждый раз, когда
                # промпт клался в кэш заново.
                written += getattr(usage, "cache_creation_input_tokens", 0) or 0

            if message.stop_reason == "max_tokens":
                truncated = True
                log.warning("ответ обрезан по лимиту в %d токенов", self.cfg.max_tokens)

            if message.stop_reason == "refusal":
                log.warning("модель отказалась отвечать: %s",
                            getattr(message, "stop_details", None))
                return "Извини, на это я ответить не могу."

            messages.append({"role": "assistant", "content": message.content})

            calls = [b for b in message.content if b.type == "tool_use"]
            if not calls:
                break

            results = []
            for call in calls:
                tool = self.tools.get(call.name)
                if tool is None:
                    log.warning("модель зовёт несуществующий инструмент %s", call.name)
                    output = f"Инструмента {call.name} не существует."
                else:
                    log.info("вызываю %s(%s)", call.name, call.input)
                    output = tool(call.input or {})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                })
            # Все результаты — одним сообщением, иначе модель разучится
            # вызывать инструменты пачкой.
            messages.append({"role": "user", "content": results})
        else:
            log.warning("исчерпан лимит вызовов инструментов за один ход (%d)", rounds)

        self.total_in += used_in + written
        self.total_out += used_out
        details = []
        if cached:
            details.append(f"из кэша {cached}")
        if written:
            details.append(f"в кэш {written}")
        log.info("токены: %d вход + %d выход%s | всего за сеанс %d + %d",
                 used_in + written, used_out,
                 f" ({', '.join(details)})" if details else "",
                 self.total_in, self.total_out)

        if truncated:
            # Иначе робот замолкает на полуслове, и понять почему нельзя:
            # человек думает, что он отвлёкся, и повторяет вопрос.
            collect(" Дальше не помещаюсь, спроси покороче.")

        self.history = _trim(messages)
        return "".join(spoken).strip()

    def reset(self) -> None:
        self.history.clear()


def _trim(messages: list[dict]) -> list[dict]:
    """Оставляет хвост истории, не разрывая пару «вызов инструмента — ответ».

    Резать можно только по настоящей реплике человека: сообщение с tool_result
    без предшествующего tool_use API отклонит.

    Если в последних HISTORY_LIMIT сообщениях реплики человека нет — а так
    бывает после хода с четырьмя и более вызовами инструментов, — ищем её
    дальше вглубь. Раньше в этом случае возвращалась вся история целиком, и
    она росла линейно: каждый тяжёлый ход добавлял по десятку сообщений,
    которые оплачивались в каждом следующем запросе.
    """
    if len(messages) <= HISTORY_LIMIT:
        return messages
    starts = [i for i, m in enumerate(messages)
              if m.get("role") == "user" and isinstance(m.get("content"), str)]
    # Самая ранняя реплика человека, после которой остаётся не больше лимита.
    for i in starts:
        if len(messages) - i <= HISTORY_LIMIT:
            return messages[i:]
    # Все ходы длиннее лимита — берём последний. Реплика человека в истории
    # есть всегда: с неё начинается каждый ход.
    return messages[starts[-1]:]
