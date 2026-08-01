"""Claude: диалог с историей, вызовом инструментов и потоковым ответом.

Ответ отдаётся по кускам, чтобы синтез речи начинался, не дожидаясь конца
генерации. Цикл «модель → инструмент → модель» ведёт tool_runner из SDK.
"""

from __future__ import annotations

import logging
from typing import Callable

import anthropic

from .config import SYSTEM_PROMPT, Config

log = logging.getLogger(__name__)

# Сколько сообщений диалога держим в контексте (пары «вопрос-ответ» плюс
# результаты инструментов). Дальше — обрезаем самые старые.
HISTORY_LIMIT = 24


class Brain:
    def __init__(self, cfg: Config, tools: list) -> None:
        self.cfg = cfg
        self.tools = tools
        self.client = anthropic.Anthropic(api_key=cfg.api_key)
        self.history: list[dict] = []
        # Отказ сервера с подстановкой запасной модели. Если SDK в системе
        # старый и не знает этих параметров — работаем без них.
        self._use_fallbacks = True

    # --- запрос ---------------------------------------------------------
    def _make_runner(self, messages: list[dict]):
        params = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=messages,
            output_config={"effort": self.cfg.effort},
            stream=True,
        )
        if self._use_fallbacks:
            try:
                return self.client.beta.messages.tool_runner(
                    **params,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except TypeError:
                log.warning("SDK не знает про fallbacks — продолжаю без них")
                self._use_fallbacks = False
        return self.client.beta.messages.tool_runner(**params)

    def reply(self, user_text: str, on_text: Callable[[str], None]) -> str:
        """Отвечает на реплику. on_text вызывается на каждый кусок текста."""
        messages = self.history + [{"role": "user", "content": user_text}]
        spoken: list[str] = []
        final = None

        runner = self._make_runner(messages)
        for stream in runner:
            for event in stream:
                if (event.type == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"):
                    spoken.append(event.delta.text)
                    on_text(event.delta.text)

            final = stream.get_final_message()
            # Ведём свою копию истории: свою runner наружу не отдаёт.
            messages.append({"role": "assistant", "content": final.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                messages.append(tool_response)

        if final is not None and final.stop_reason == "refusal":
            log.warning("модель отказалась отвечать: %s", final.stop_details)
            return "Извини, на это я ответить не могу."

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
