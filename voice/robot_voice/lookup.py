"""Справки из интернета: Википедия, курс валют, радиостанции.

Общее у всех трёх — ни ключей, ни регистрации, ни оплаты. Это было условием:
домашний робот не должен зависеть от чужой подписки, которую однажды забудут
продлить.

Почему не «поиск в интернете». Настоящего бесплатного поискового API без
ключа не существует: у Google и Bing он платный, у Brave и Tavily бесплатный
тариф упирается в карту и лимиты. Зато для домашних вопросов — «кто такой»,
«что такое», «сколько стоит доллар» — поиск и не нужен. Нужна справка, и её
дают Википедия и Центробанк, оба открыто и без счёта.

Отвечаем коротко: две-три фразы. Робот говорит вслух, и абзац из энциклопедии
превращается в полторы минуты, которые никто не дослушает.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT = 8.0
AGENT = "kuzya-robot/1.0 (домашний робот)"

WIKI = "https://ru.wikipedia.org/w/api.php"
DDG = "https://api.duckduckgo.com/"
# Центробанк отдаёт курсы обычным json, без ключа и регистрации. Зеркало
# cbr-xml-daily держит их в удобном виде и обновляется раз в сутки.
RATES = "https://www.cbr-xml-daily.ru/daily_json.js"

# Сколько предложений справки зачитывать. Три — предел того, что человек
# удерживает на слух; дальше он всё равно переспросит своими словами.
SENTENCES = 3


def _get(url: str, params: dict | None = None) -> dict | None:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("%s не ответил: %s", url.split("/")[2], e)
        return None


def _short(text: str) -> str:
    """Обрезает справку до нескольких предложений, не рубя посередине."""
    text = re.sub(r"\s+", " ", re.sub(r"\([^)]{0,80}\)", "", text)).strip()
    куски = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(куски[:SENTENCES]).strip()


def wikipedia(query: str) -> str:
    """Первый абзац статьи. Пусто — не нашли."""
    ответ = _get(WIKI, {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrlimit": 1,
        "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
    })
    страницы = ((ответ or {}).get("query") or {}).get("pages") or {}
    for page in страницы.values():
        текст = _short(page.get("extract") or "")
        if текст:
            return текст
    return ""


def duckduckgo(query: str) -> str:
    """Короткая справка от DuckDuckGo. Пусто — у него нечего сказать.

    Полноценного поиска он не отдаёт — только «мгновенные ответы» из
    справочников. Держим вторым номером: там, где Википедия промолчала, он
    иногда знает.
    """
    ответ = _get(DDG, {"q": query, "format": "json", "no_html": 1,
                       "skip_disambig": 1, "kl": "ru-ru"})
    return _short((ответ or {}).get("AbstractText") or "")


def find(query: str) -> str:
    """Справка по вопросу. Что скажет робот вслух."""
    query = " ".join(query.split())
    if len(query) < 3:
        return "А про что посмотреть?"
    for источник in (wikipedia, duckduckgo):
        текст = источник(query)
        if текст:
            return текст
    return f"Не нашёл ничего про «{query}»."


def rates() -> str:
    """Курс доллара и евро по Центробанку."""
    ответ = _get(RATES)
    валюты = (ответ or {}).get("Valute") or {}
    if not валюты:
        return "Курс сейчас не отвечает, попробуй позже."
    куски = []
    for код, имя in (("USD", "доллар"), ("EUR", "евро")):
        цена = (валюты.get(код) or {}).get("Value")
        if цена:
            # Копейки вслух не нужны: «семьдесят девять рублей» человек
            # запомнит, «семьдесят девять и сорок три» — нет.
            куски.append(f"{имя} {round(цена)}")
    if not куски:
        return "Курс сейчас не отвечает, попробуй позже."
    return "По Центробанку: " + ", ".join(куски) + " рублей."
