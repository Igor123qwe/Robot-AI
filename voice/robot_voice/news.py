"""Новости вслух: несколько заголовков из ленты RSS.

Почему RSS, а не поиск в интернете. Робот отвечает голосом, и человеку нужны
три-четыре заголовка, а не выдача из десяти ссылок. RSS даёт ровно это: список
свежих заголовков, без ключей, без оплаты и без разбора чужой вёрстки. Разбор
идёт стандартным xml из поставки питона — ставить ничего не нужно.

Зачитывается только заголовок. Описание в лентах — это первый абзац статьи,
и вслух он превращается в минуту монотонного бубнежа; если человеку интересно,
он спросит подробнее, и тогда за дело возьмётся модель.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

# Лента по умолчанию. Меняется настройкой ROBOT_NEWS_URL: подойдёт любая RSS,
# в том числе районная или отраслевая.
URL = "https://lenta.ru/rss/news"

TIMEOUT = 8.0

# Сколько заголовков зачитывать. Больше пяти вслух не запоминается, а робот
# всё это время глухой: микрофон заглушён, пока он говорит.
COUNT = 4

# Хвосты, которыми ленты подписывают заголовки: «— РИА Новости», «| Лента».
# Вслух они звучат как заикание, потому что повторяются в каждом.
_TAIL = re.compile(r"\s*[|—–-]\s*(?:риа\s+новости|лента\.?\s*ру|интерфакс|"
                   r"тасс|коммерсантъ|rbc|рбк)\s*$", re.I)


def _clean(title: str) -> str:
    """Заголовок, годный для произнесения вслух."""
    title = re.sub(r"<[^>]+>", " ", title)
    title = " ".join(title.split())
    title = _TAIL.sub("", title).strip(" .")
    return title


def headlines(url: str = URL, count: int = COUNT) -> list[str]:
    """Свежие заголовки. Пустой список — лента не ответила."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "kuzya-robot/1.0"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            корень = ET.fromstring(resp.read())
    except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as e:
        log.warning("новости не ответили: %s", e)
        return []

    titles: list[str] = []
    # RSS кладёт новости в channel/item, Atom — в entry. Поддерживаем оба: не
    # угадаешь, что человек впишет в настройку.
    for item in корень.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        for child in item:
            if child.tag.rsplit("}", 1)[-1] == "title" and (child.text or "").strip():
                чистый = _clean(child.text)
                if чистый:
                    titles.append(чистый)
                break
        if len(titles) >= count:
            break
    return titles


def describe(titles: list[str]) -> str:
    """Что робот скажет вслух."""
    if not titles:
        return "Новости сейчас не отвечают, попробуй позже."
    # Нумерация вслух не нужна: человек и так слышит паузы между фразами, а
    # «раз, два, три» превращает сводку в перекличку.
    return "Вот что пишут. " + " ".join(t.rstrip(".") + "." for t in titles)
