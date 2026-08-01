"""Конфигурация из окружения (~/.robot-ai.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    # --- модель ---
    api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    # Пусто — идём напрямую в api.anthropic.com. Иначе адрес роутера,
    # понимающего протокол Anthropic (проверяется запросом к /v1/messages).
    api_base: str = field(default_factory=lambda: _env("ROBOT_API_BASE"))
    model: str = field(default_factory=lambda: _env("ROBOT_MODEL", "claude-opus-5"))
    # low/medium держат ответ быстрым — для разговора это важнее глубины.
    # Пусто — не отправлять вовсе: сторонний роутер может этот параметр не знать.
    effort: str = field(default_factory=lambda: _env("ROBOT_EFFORT", "low"))
    max_tokens: int = field(default_factory=lambda: int(_env("ROBOT_MAX_TOKENS", "2048")))

    # --- аудио ---
    # phone — Android с IP Webcam, local — микрофон в RDK X5
    audio_source: str = field(default_factory=lambda: _env("ROBOT_AUDIO_SOURCE", "phone"))
    phone_url: str = field(default_factory=lambda: _env("ROBOT_PHONE_URL", "http://192.168.0.77:8080"))
    sample_rate: int = 16000

    # Порог тишины, после которого считаем фразу законченной.
    silence_ms: int = 700
    # Не реагируем на щелчки: фраза короче этого — мусор.
    min_speech_ms: int = 300
    # Агрессивность webrtcvad: 0 мягко, 3 жёстко (меньше ложных срабатываний).
    vad_level: int = 2

    # --- распознавание ---
    whisper_model: str = field(default_factory=lambda: _env("ROBOT_WHISPER_MODEL", "base"))
    language: str = "ru"

    # --- синтез ---
    piper_voice: str = field(default_factory=lambda: _env("ROBOT_PIPER_VOICE", "ru_RU-irina-medium"))
    # browser — говорит браузер с открытым пультом (пока нет своего динамика),
    # local — сразу в aplay (когда приедет SOTAMIA).
    audio_out: str = field(default_factory=lambda: _env("ROBOT_AUDIO_OUT", "browser"))
    web_endpoint: str = field(
        default_factory=lambda: _env("ROBOT_WEB_URL", "http://127.0.0.1:8000") + "/speak")

    # --- ROS ---
    rosbridge_url: str = field(default_factory=lambda: _env("ROBOT_ROSBRIDGE_URL", "ws://127.0.0.1:9090"))

    # --- слово пробуждения ---
    # Пока микрофон на телефоне, аппаратного AEC нет и barge-in не работает,
    # поэтому просто требуем обращения по имени в начале фразы.
    # Пустая строка = слушать всё подряд.
    wake_words: tuple[str, ...] = ("робот", "роберт", "робик")

    @property
    def piper_model_path(self) -> Path:
        return VOICE_DIR / "models" / f"{self.piper_voice}.onnx"

    def check(self) -> None:
        if not self.api_key:
            raise SystemExit(
                "не задан ANTHROPIC_API_KEY — заполните ~/.robot-ai.env"
            )
        if self.audio_source not in ("phone", "local"):
            raise SystemExit(f"ROBOT_AUDIO_SOURCE должен быть phone или local, а не {self.audio_source!r}")
        if self.audio_out not in ("browser", "local"):
            raise SystemExit(f"ROBOT_AUDIO_OUT должен быть browser или local, а не {self.audio_out!r}")


SYSTEM_PROMPT = """\
Ты — домашний робот-ассистент Игоря. Физически ты — небольшая тележка на \
четырёх меканум-колёсах, которая ездит по квартире.

Как говорить:
- Отвечай по-русски, коротко, живой разговорной речью. Твой ответ озвучивается \
вслух, поэтому не используй списки, заголовки, разметку, эмодзи и скобки с \
пояснениями — только то, что естественно звучит голосом.
- Одна-две фразы в обычном случае. Развёрнуто — только если попросили объяснить.
- Числа пиши словами, когда так естественнее звучит.

Как действовать:
- Ты умеешь ездить, разворачиваться, останавливаться, проверять заряд батареи и \
ставить таймеры. Для этого у тебя есть инструменты — вызывай их, а не описывай \
словами, что ты якобы сделал.
- Если команда двусмысленная и разные толкования приведут к разным действиям — \
переспроси одной фразой.
- Перед движением коротко скажи, что делаешь.
- Если тебя просят ехать, а заряд ниже пятнадцати процентов — предупреди об этом.
- Не выдумывай, что видишь вокруг: камеры у тебя пока нет.
"""
