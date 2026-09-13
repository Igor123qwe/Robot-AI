"""Провайдер-заглушка для Open-LLM-VTuber: не думает — спрашивает у робота.

КУДА КЛАСТЬ (в дереве Open-LLM-VTuber): рядом с ollama_llm.py, claude_llm.py —
    src/open_llm_vtuber/agent/stateless_llm/robot_bridge_llm.py

Дальше — три правки в самом Open-LLM-VTuber (иначе conf.yaml не пройдёт
проверку настроек и провайдер не найдётся): см. pc/ollv_bridge/README.md.

ЧТО ДЕЛАЕТ. Вместо вызова настоящей модели опрашивает мост на ПК
(pc/ollv_bridge.py в этом репозитории — тот же сервер и порт, что /tts и
/avatar/state): что сейчас говорит робот. И просто пересказывает это слово
в слово, с тегом эмоции вроде [joy] впереди — его вырежет и превратит в
выражение модели их собственный live2d_model.py, без нашего участия.

Разговор с этим провайдером начинает не человек, а их штатный сигнал
ai-speak-signal («персонажу есть что сказать») — pc/ollv_bridge.py шлёт
его сам, как только у робота готова новая фраза. Тот текст, что придёт
сюда в `messages` (обычно "Please say something." — их проактивная
подсказка), эта заглушка не читает: у неё уже есть, что сказать, от
настоящего мозга робота (voice/robot_voice/brain.py).
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import httpx
from loguru import logger

from .stateless_llm_interface import StatelessLLMInterface


class RobotBridgeLLM(StatelessLLMInterface):
    def __init__(self, bridge_url: str = "http://127.0.0.1:4000", timeout: float = 3.0) -> None:
        # Тот же адрес и порт, что кормит /tts и /avatar/state на ПК —
        # значение "--port" из запуска kuzya_pc.py.
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout = timeout

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        system: str = None,
        tools: List[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                ответ = await client.get(f"{self.bridge_url}/ollv/line")
                данные = ответ.json()
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"RobotBridgeLLM: мост недоступен ({e})")
            return
        текст = (данные or {}).get("текст") or ""
        if not текст:
            # Сигнал пришёл, а мост уже пуст (фраза устарела, гонка при
            # старте) — молчим, а не выдумываем, что сказать.
            return
        yield текст
