"""TTS-заглушка для Open-LLM-VTuber: не синтезирует — забирает готовый звук.

КУДА КЛАСТЬ (в дереве Open-LLM-VTuber):
    src/open_llm_vtuber/tts/robot_bridge_tts.py

ЗАЧЕМ. Голос уже синтезирован — тот же самый WAV, что в этот момент звучит
из колонки робота (pc/kuzya_pc.py, обработчик /tts). Синтезировать его
ЕЩЁ РАЗ значит вдвое нагрузить видеокарту и получить ДВА РАЗНЫХ звука,
которые разойдутся по времени с тем, что человек слышит вживую. Этот
провайдер просто забирает тот же WAV с моста (pc/ollv_bridge.py) —
Open-LLM-VTuber сам посчитает по нему огибающую громкости и синхронизирует
рот, ему для этого не нужно ничего, кроме файла.
"""

from __future__ import annotations

import requests
from loguru import logger

from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
    def __init__(self, bridge_url: str = "http://127.0.0.1:4000", timeout: float = 5.0) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout = timeout

    def generate_audio(self, text: str, file_name_no_ext: str | None = None) -> str | None:
        file_name = self.generate_cache_file_name(file_name_no_ext, "wav")
        try:
            ответ = requests.get(f"{self.bridge_url}/ollv/audio", timeout=self.timeout)
            ответ.raise_for_status()
        except Exception as e:                       # noqa: BLE001
            logger.error(f"RobotBridgeTTS: мост недоступен ({e})")
            return None
        with open(file_name, "wb") as f:
            f.write(ответ.content)
        return file_name
