"""Конфигурация из окружения (~/.robot-ai.env)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

VOICE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _num(name: str, default: float) -> float:
    """Число из окружения. Пустая строка и мусор — это «не задано».

    В env-файле легко оставить `ROBOT_VOLUME=` пустым, и раньше сервис от
    этого падал при старте с ValueError, а робот молча не поднимался.
    """
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        log.warning("%s=%r — не число, беру %s", name, raw, default)
        return default


def _api_base(raw: str) -> str:
    """Приводит адрес API к тому, что ждёт SDK.

    Клиент Anthropic сам дописывает /v1/messages. Естественно указать адрес
    роутера ровно так, как он написан в его документации — «…/api/v1», — и
    получить /api/v1/v1/messages и 404. Поэтому хвостовой /v1 срезаем.
    """
    base = raw.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


@dataclass
class Config:
    # --- модель ---
    api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    # Пусто — идём напрямую в api.anthropic.com. Иначе адрес роутера,
    # понимающего протокол Anthropic (проверяется запросом к /v1/messages).
    api_base: str = field(default_factory=lambda: _api_base(_env("ROBOT_API_BASE")))
    model: str = field(default_factory=lambda: _env("ROBOT_MODEL", "claude-opus-5"))
    # low/medium держат ответ быстрым — для разговора это важнее глубины.
    # Пусто — не отправлять вовсе: сторонний роутер может этот параметр не знать.
    effort: str = field(default_factory=lambda: _env("ROBOT_EFFORT", "low"))
    max_tokens: int = field(default_factory=lambda: int(_num("ROBOT_MAX_TOKENS", 2048)))

    # --- аудио ---
    # phone — Android с IP Webcam, local — микрофон в RDK X5,
    # browser — микрофон компьютера через вкладку пульта
    audio_source: str = field(default_factory=lambda: _env("ROBOT_AUDIO_SOURCE", "phone"))
    phone_url: str = field(default_factory=lambda: _env("ROBOT_PHONE_URL", "http://192.168.0.77:8080"))
    sample_rate: int = 16000

    # Порог тишины, после которого считаем фразу законченной.
    silence_ms: int = 700
    # Не реагируем на щелчки: фраза короче этого — мусор.
    min_speech_ms: int = 300
    # Предохранитель от бесконечной записи, если VAD залип на шуме.
    max_speech_ms: int = 20000
    # Агрессивность webrtcvad: 0 мягко, 3 жёстко (меньше ложных срабатываний).
    vad_level: int = 2

    # --- распознавание ---
    whisper_model: str = field(default_factory=lambda: _env("ROBOT_WHISPER_MODEL", "base"))
    # 1 — жадный поиск, быстро. 5 — точнее, но втрое дольше на A55.
    whisper_beam: int = field(default_factory=lambda: int(_num("ROBOT_WHISPER_BEAM", 1)))
    language: str = "ru"

    # --- синтез ---
    piper_voice: str = field(default_factory=lambda: _env("ROBOT_PIPER_VOICE", "ru_RU-irina-medium"))
    # browser — говорит браузер с открытым пультом (пока нет своего динамика),
    # local — сразу в aplay (когда приедет SOTAMIA).
    audio_out: str = field(default_factory=lambda: _env("ROBOT_AUDIO_OUT", "browser"))
    web_endpoint: str = field(
        default_factory=lambda: _env("ROBOT_WEB_URL", "http://127.0.0.1:8000") + "/speak")

    # Громкость по умолчанию, 0.1–1.0. Меняется голосом («говори тише») и
    # переживает перезапуск: настройка ночью не должна откатываться сама.
    volume: float = field(
        default_factory=lambda: _num("ROBOT_VOLUME", 1.0))
    # Тихие часы: в это время робот говорит вполголоса и не здоровается при
    # перезапуске. Будильник и таймер звучат в полную громкость — их для того
    # и ставили. Пусто — режим выключен.
    quiet_hours: str = field(default_factory=lambda: _env("ROBOT_QUIET_HOURS", "23-8"))
    quiet_volume: float = field(
        default_factory=lambda: _num("ROBOT_QUIET_VOLUME", 0.45))

    # --- ROS ---
    rosbridge_url: str = field(default_factory=lambda: _env("ROBOT_ROSBRIDGE_URL", "ws://127.0.0.1:9090"))

    # Команду движения выполняем только по имени. В разговоре без имени робот
    # ездить не будет: пока окно открыто, любая фраза из комнаты — телевизор,
    # разговор с другим человеком — выглядит как команда, а «поехал» отменить
    # уже нельзя. Остановка, таймеры и вопросы имя не требуют.
    motion_needs_name: bool = field(
        default_factory=lambda: _env("ROBOT_MOTION_NEEDS_NAME", "1") not in ("0", "нет", "no"))

    # Где хранится то, что должно пережить перезапуск сервиса. Пустая строка в
    # env-файле — это «не задано», а не «текущий каталог»: иначе таймеры и
    # список покупок легли бы внутрь репозитория, откуда их сметёт обновление.
    data_dir: Path = field(default_factory=lambda: Path(
        _env("ROBOT_DATA_DIR") or str(Path.home() / ".robot-ai")))

    # Координаты дома для погоды. Пусто — робот честно скажет, что не знает,
    # где находится: врать про погоду в чужом городе хуже, чем молчать.
    lat: float = field(default_factory=lambda: _num("ROBOT_LAT", 0.0))
    lon: float = field(default_factory=lambda: _num("ROBOT_LON", 0.0))

    # --- имя и режим разговора ---
    # Робот слушает всегда, но реагирует только на своё имя. После обращения
    # он остаётся «проснувшимся» и отвечает без имени, пока идёт разговор, —
    # как Алиса. Через паузу засыпает обратно.
    #
    # Вариантов имени несколько: Whisper пишет «Кузя», «Кузь», «Куся» и прочее.
    # Похожие слова ловятся нестрого, поэтому список — только опоры.
    wake_words: tuple[str, ...] = field(default_factory=lambda: tuple(
        w.strip().lower().replace("ё", "е")
        for w in _env("ROBOT_WAKE_WORDS", "кузя,кузь,куся,кузи,кузьма").split(",")
        if w.strip()))
    # Сколько секунд после ответа робот продолжает слушать без имени.
    session_seconds: float = field(
        default_factory=lambda: _num("ROBOT_SESSION_SECONDS", 20))
    # Порог похожести имени: 4 буквы — короткое слово, послабление опасно.
    wake_ratio: float = field(
        default_factory=lambda: _num("ROBOT_WAKE_RATIO", 0.8))
    # Чем отпустить робота, не дожидаясь паузы. «Хватит» и «стоп» сюда не
    # попали намеренно: это команды остановить движение, а не закончить разговор.
    # «Забудь», «неважно», «проехали» — из интента HassNevermind Home Assistant:
    # человек передумал, и правильная реакция — замолчать, а не спрашивать модель.
    # «Отбой» сюда не входит: это команда остановки из правила _stop, и
    # проверяется она раньше. Иначе «отбой!» на ходу усыплял бы робота, а
    # ехать он продолжал.
    # Имя снимается до этой проверки, поэтому фразы с именем сюда писать
    # бесполезно: «спасибо, Кузя» доходит сюда как «спасибо».
    sleep_words: tuple[str, ...] = ("спасибо", "свободен", "спи",
                                    "все", "иди спи",
                                    "забудь", "забей", "неважно", "не важно",
                                    "проехали", "отмена", "ничего")

    @property
    def piper_model_path(self) -> Path:
        return VOICE_DIR / "models" / f"{self.piper_voice}.onnx"

    def is_quiet_now(self, hour: int | None = None) -> bool:
        """Ночь ли сейчас по настройке ROBOT_QUIET_HOURS («23-8»)."""
        if not self.quiet_hours:
            return False
        try:
            start, end = (int(x) for x in self.quiet_hours.split("-", 1))
        except ValueError:
            return False
        if hour is None:
            hour = datetime.now().hour
        # Окно почти всегда переходит через полночь, поэтому две ветки.
        return start <= hour or hour < end if start > end else start <= hour < end

    def check(self) -> None:
        if not self.api_key:
            raise SystemExit(
                "не задан ANTHROPIC_API_KEY — заполните ~/.robot-ai.env"
            )
        if self.audio_source not in ("phone", "local", "browser"):
            raise SystemExit("ROBOT_AUDIO_SOURCE должен быть phone, local или "
                             f"browser, а не {self.audio_source!r}")
        if self.audio_out not in ("browser", "local"):
            raise SystemExit(f"ROBOT_AUDIO_OUT должен быть browser или local, а не {self.audio_out!r}")


SYSTEM_PROMPT = """\
Тебя зовут Кузя. Ты — домашний робот-ассистент Игоря. Физически ты — небольшая \
тележка на четырёх меканум-колёсах, которая ездит по квартире.

Как говорить:
- Отвечай по-русски, коротко, живой разговорной речью. Твой ответ озвучивается \
вслух, поэтому не используй списки, заголовки, разметку, эмодзи и скобки с \
пояснениями — только то, что естественно звучит голосом.
- Одна-две фразы в обычном случае. Развёрнуто — только если попросили объяснить.
- Числа пиши словами, когда так естественнее звучит.

Как действовать:
- Ты умеешь ездить, разворачиваться, останавливаться, проверять заряд батареи, \
ставить таймеры и будильники, напоминать о делах, вести список покупок, \
называть время и дату, говорить тише и громче. Для этого у тебя есть \
инструменты — вызывай их, а не описывай словами, что ты якобы сделал.
- Часов и календаря у тебя своих нет: время и дату спрашивай инструментом, \
а не отвечай по памяти. Ошибиться тут стыдно.
- Если команда двусмысленная и разные толкования приведут к разным действиям — \
переспроси одной фразой.
- Перед движением коротко скажи, что делаешь.
- Если тебя просят ехать, а заряд ниже пятнадцати процентов — предупреди об этом.
- Не выдумывай, что видишь вокруг: камеры у тебя пока нет.
- Умного дома, музыки, почты и календаря у тебя нет. Просят их — скажи об \
этом прямо и коротко, без извинений на три предложения.
"""
