"""Поиск мультиков/видео на YouTube — без токенов и аккаунта.

Пробовали официальный API VK Видео: `video.search` требует пользовательский
OAuth-токен, а получить его к 2026 году стало отдельным квестом (VK убрал
старый тип приложений «Standalone», новый OAuth сложнее) — и, что важнее,
нашлись свежие сообщения о заморозке аккаунтов за использование этого метода
из сторонних приложений. Рисковать личным VK-аккаунтом ради мультиков не
стоит.

YouTube проще и безопаснее именно потому, что ничего не просит: `yt-dlp` —
свободная утилита, которую не нужно авторизовывать, она просто ищет и отдаёт
ссылку. Мультик потом играет через `mpv` (см. face/videoplayer.py) с его
собственной поддержкой YouTube — тем же способом, каким сейчас играет музыка
(voice/robot_voice/music.py). Никакой поддержки ПК для этого не нужно.

Единственное условие — YouTube должен открываться из дома без VPN. Если нет,
искать эту функцию смысла нет: без сети играть нечего.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Поиск идёт синхронно внутри хода модели — человек в этот момент ждёт ответа
# молча. У yt-dlp свой таймаут по умолчанию (и свои повторы поверх него),
# который ничем не ограничен сверху и может растянуться сильно дольше, чем
# любой другой сетевой путь в этом файле (ср. weather.py TIMEOUT=8,
# camera.py ОТВЕТ_ЖДЁМ=20) — а значит и дольше, чем человек готов молчать.
ТАЙМАУТ = 10.0


def possible() -> bool:
    """Есть ли чем искать. Нет yt-dlp — нет и поиска, а не падение."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def search(что: str, сколько: int = 8) -> list[tuple[str, str]]:
    """Первые «сколько» роликов по запросу: [(адрес, название), ...].

    Пустой список — не нашёл или yt-dlp не установлен. Адрес — обычная
    страница https://www.youtube.com/watch?v=..., её понимает mpv сам, через
    встроенный ytdl_hook: разбирать формат/качество здесь не нужно.
    """
    что = " ".join((что or "").split())
    if not что:
        return []
    try:
        import yt_dlp
    except ImportError:
        log.warning("видео: yt-dlp не установлен")
        return []
    сколько = max(1, min(20, int(сколько)))
    опции = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "socket_timeout": ТАЙМАУТ,
    }
    try:
        with yt_dlp.YoutubeDL(опции) as ydl:
            данные = ydl.extract_info(f"ytsearch{сколько}:{что}", download=False)
    except Exception as e:                       # noqa: BLE001
        log.warning("видео: поиск «%s» не вышел (%s)", что, e)
        return []
    записи = (данные or {}).get("entries") or []
    результат = []
    for запись in записи:
        if not запись:
            continue
        id_ = запись.get("id")
        if not id_:
            continue
        название = str(запись.get("title") or "").strip()
        результат.append((f"https://www.youtube.com/watch?v={id_}", название))
    return результат
