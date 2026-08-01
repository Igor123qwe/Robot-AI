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
# инструментов). Дальше обрезаем самые старые.
HISTORY_LIMIT = 24

# Предохранитель от зацикливания: модель не должна дёргать инструменты вечно.
MAX_TOOL_ROUNDS = 6


class Brain:
    def __init__(self, cfg: Config, tools: list[Tool]) -> None:
        self.cfg = cfg
        self.tools = {t.name: t for t in tools}
        self.specs = [t.spec() for t in tools]

        client_args = {"api_key": cfg.api_key}
        if cfg.api_base:
            client_args["base_url"] = cfg.api_base
        self.client = anthropic.Anthropic(**client_args)

        self.history: list[dict] = []

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
        return params

    # --- один ход --------------------------------------------------------
    def reply(self, user_text: str, on_text: Callable[[str], None]) -> str:
        """Отвечает на реплику. on_text вызывается на каждый кусок текста."""
        messages = self.history + [{"role": "user", "content": user_text}]
        spoken: list[str] = []

        for round_no in range(MAX_TOOL_ROUNDS):
            with self.client.messages.stream(**self._params(messages)) as stream:
                for event in stream:
                    if (event.type == "content_block_delta"
                            and getattr(event.delta, "type", "") == "text_delta"):
                        spoken.append(event.delta.text)
                        on_text(event.delta.text)
                message = stream.get_final_message()

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
            log.warning("исчерпан лимит вызовов инструментов за один ход")

        self.history = _trim(messages)
        return "".join(spoken).strip()

    def reset(self) -> None:
        self.history.clear()


def _trim(messages: list[dict]) -> list[dict]:
    """Оставляет хвост истории, не разрывая пару «вызов инструмента — ответ».

    Резать можно только по настоящей реплике человека: сообщение с tool_result
    без предшествующего tool_use API отклонит.
    """
    if len(messages) <= HISTORY_LIMIT:
        return messages
    for i in range(len(messages) - HISTORY_LIMIT, len(messages)):
        m = messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return messages[i:]
    return messages
