"""Радио: поиск станции и её поток.

Своей музыкальной библиотеки у робота нет и не предвидится — карта памяти на
шасси не под то. Зато есть интернет-радио: тысячи станций, живой поток, ни
ключей, ни подписки. «Кузя, включи джаз» — и играет джаз.

Каталог берём у radio-browser.info: открытый, общественный, без регистрации.
Ищем и по названию («включи Рекорд»), и по жанру («включи джаз») — человек
скажет и так, и так, а угадывать, что он имел в виду, дешевле здесь, чем
объяснять ему разницу.

Играет поток в пульте: у робота своего динамика пока нет, а вкладка умеет
тянуть радио сама, без нашего участия. Робот только называет адрес.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# Круговой адрес каталога: за ним стоит несколько зеркал, и если одно легло,
# отвечает другое. Ключа не нужно, но представиться просят — по имени они
# считают статистику и отсеивают ботов.
API = "https://all.api.radio-browser.info/json/stations/search"
AGENT = "kuzya-robot/1.0"
TIMEOUT = 8.0

# Сколько станций просить у каталога. Берём с запасом и выбираем сами:
# половина ссылок в открытом каталоге мертва или отдаёт тишину.
ASK = 20

# Ниже этого битрейта звук уже неприятный: шипение вместо музыки.
DECENT = 64

# Браузер играет не всё. MP3 понимают все и всегда; AAC+ (в каталоге это
# codec=AAC+ и адреса вида .aacp) Chrome тянет через раз, а Firefox не тянет
# вовсе. На живом роботе именно на этом всё и встало: станция нашлась, адрес
# уехал в пульт, робот отрапортовал «музыка включена» — и тишина.
PLAYABLE = ("MP3",)


def _search(**params) -> list[dict]:
    query = urllib.parse.urlencode({
        "limit": ASK, "hidebroken": "true",
        "order": "clickcount", "reverse": "true", **params})
    try:
        request = urllib.request.Request(f"{API}?{query}",
                                         headers={"User-Agent": AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            found = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("каталог радио не ответил: %s", e)
        return []
    return found if isinstance(found, list) else []


def _playable(station: dict) -> bool:
    """Сможет ли браузер это проиграть."""
    codec = (station.get("codec") or "").upper()
    адрес = (station.get("url_resolved") or station.get("url") or "").lower()
    if codec and codec not in PLAYABLE:
        return False
    # Кодек в каталоге проставлен не у всех, поэтому смотрим и на адрес.
    return not адрес.split("?")[0].endswith((".aacp", ".aac", ".m3u8", ".ogg"))


def _best(stations: list[dict]) -> dict | None:
    """Лучшая из найденных: живая, играбельная, с приличным звуком."""
    годные = [s for s in stations
              if (s.get("url_resolved") or s.get("url"))
              and int(s.get("lastcheckok") or 0) == 1
              and _playable(s)]
    if not годные:
        return None
    # Сначала приличный битрейт, потом популярность. Каталог уже отсортирован
    # по числу включений, поэтому достаточно устойчивой сортировки по звуку.
    # Нулевой битрейт в каталоге значит «неизвестно», а не «плохо».
    годные.sort(key=lambda s: int(s.get("bitrate") or DECENT) >= DECENT,
                reverse=True)
    return годные[0]


def find(what: str, language: str = "russian") -> tuple[str, str] | None:
    """Станция по названию или жанру. Возвращает (имя, адрес потока).

    Порядок поиска важен. Сначала по названию: «включи Рекорд» — это про
    конкретную станцию, и жанровый поиск выдал бы вместо неё случайный
    танцевальный поток. Не нашлось — ищем по жанру, сначала среди русских
    станций, потом среди любых: «включи джаз» на русском языке не обязано
    находиться, а джаз интернациональн.
    """
    what = " ".join(what.split())
    if not what:
        return None
    # codec=MP3 просим прямо у каталога: так меньше шансов, что все двадцать
    # найденных окажутся в AAC и выбирать будет не из чего.
    попытки = (
        {"name": what, "codec": "MP3"},
        {"tag": what, "language": language, "codec": "MP3"},
        {"tag": what, "codec": "MP3"},
        {"name": what},
        {"tag": what},
    )
    for params in попытки:
        станция = _best(_search(**params))
        if станция:
            имя = " ".join((станция.get("name") or "радио").split())
            поток = станция.get("url_resolved") or станция.get("url")
            log.info("радио: %s (%s, %s кбит) → %s", имя,
                     станция.get("codec") or "кодек неизвестен",
                     станция.get("bitrate"), поток)
            return имя, поток
    return None
