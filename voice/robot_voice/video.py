"""Поиск мультиков/видео — Rutube по умолчанию, YouTube по желанию.

ПОЧЕМУ RUTUBE. YouTube из дома хозяина не отвечает: соединение открывается,
данных нет («Read timed out» на каждом запросе к API). Это не робот и не
yt-dlp — так YouTube выглядит из России без VPN, и в шапке этого файла
условие «должен открываться из дома» стояло первым с самого начала. Rutube
открывается, у него открытый JSON-API без токена и аккаунта, и — что важно
для робота без видеокарты — прямой HLS-адрес ролика отдаётся отдельным
запросом: mpv играет его сам, без yt-dlp вовсе. yt-dlp на роботе стоит на
Python 3.10, который он уже объявил устаревшим, — одной хрупкой зависимостью
на пути к мультику меньше.

API взято не с потолка, а из разборщика Rutube в самом yt-dlp
(yt_dlp/extractor/rutube.py): списки отдают `results[]` с `video_url`,
`title`, `is_adult`; `api/play/options/<id>/` отдаёт `video_balancer` с
`m3u8`. Поиск — тот же список по адресу `api/search/video/?query=`.

Про VK Видео: `video.search` требует пользовательский OAuth-токен, а
получить его стало отдельным квестом, и есть свежие сообщения о заморозке
аккаунтов за использование этого метода из сторонних приложений. Рисковать
личным VK-аккаунтом ради мультиков не стоит.

YouTube остаётся за флагом ROBOT_VIDEO_SOURCE=youtube: там ищет yt-dlp
(`pip install yt-dlp`), играет mpv через свой ytdl_hook. Условие прежнее —
YouTube должен открываться из дома без VPN.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# Откуда брать мультики. rutube — умолчание, см. шапку; youtube — по флагу.
ИСТОЧНИК = (os.environ.get("ROBOT_VIDEO_SOURCE", "rutube").strip().lower()
            or "rutube")
ИМЯ_ИСТОЧНИКА = "YouTube" if ИСТОЧНИК == "youtube" else "Rutube"

# Поиск идёт синхронно внутри хода модели — человек в этот момент ждёт ответа
# молча, а микрофон копит кадры, которые некому разбирать. Поэтому у поиска
# ДВА предела, и оба обязательны.
#
# Первый — на один сетевой запрос: socket_timeout. Он тут стоял давно, и
# комментарий рядом честно знал, что «у yt-dlp свои повторы поверх него,
# ничем не ограниченные сверху», — а повторы при этом не ограничивал никто.
# Живой случай: YouTube из дома не отвечает (читать API не даёт), yt-dlp
# упирается в десять секунд и пробует снова, и снова — «ответ 82.4 с»,
# «инструмент 82390 мс», и всё это время голосовой цикл стоит: «не успеваю
# разбирать, выброшено 2500 кадров». Человек полторы минуты ждал «Не нашёл:
# мультики». Повторы — в ноль: не ответил с первого раза — не ответит и с
# восьмого, а полторы минуты молчания хуже честного «не отвечает».
ТАЙМАУТ = 10.0
# Второй — на весь поиск целиком, что бы yt-dlp внутри ни делал: разбор
# страницы, подбор клиента, DNS. Отдельный поток и срок; не уложился — так
# и говорим. Брошенный поток доработает в фоне и умрёт сам.
ТАЙМАУТ_ВСЕГО = 15.0

# Rutube отвечает обычному браузеру; голому urllib — тоже, но представляться
# браузером надёжнее: прокси и защиты от ботов смотрят на этот заголовок.
ЗАГОЛОВКИ = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Robot-AI/1.0",
             "Accept": "application/json"}
RUTUBE_ПОИСК = "https://rutube.ru/api/search/video/"
RUTUBE_ВОСПРОИЗВЕДЕНИЕ = "https://rutube.ru/api/play/options/{id}/"


class Недоступен(RuntimeError):
    """Видеосервис не отвечает. Это не «не нашёл» — искать было негде."""


def possible() -> bool:
    """Есть ли чем искать. Rutube — всегда (только urllib); YouTube — если
    стоит yt-dlp. Нет — не падение, а честное «искать нечем»."""
    if ИСТОЧНИК != "youtube":
        return True
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def _читать_json(url: str) -> dict:
    """Один GET с одним таймаутом. Сетевая беда — Недоступен, не список.

    Отдельной функцией, чтобы проверки подменяли ровно сеть, а не urllib
    целиком. Обрезок ответа в исключении — для журнала: если Rutube сменит
    форму API, по журналу это будет видно, а не по «не нашёл».
    """
    запрос = urllib.request.Request(url, headers=ЗАГОЛОВКИ)
    try:
        with urllib.request.urlopen(запрос, timeout=ТАЙМАУТ) as ответ:
            сырое = ответ.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Недоступен(f"{ИМЯ_ИСТОЧНИКА}: {e}") from None
    try:
        данные = json.loads(сырое.decode("utf-8", "replace"))
    except ValueError:
        raise Недоступен(f"{ИМЯ_ИСТОЧНИКА} прислал не JSON: "
                         f"{сырое[:80]!r}") from None
    return данные if isinstance(данные, dict) else {}


def _искать_rutube(что: str, сколько: int) -> list[tuple[str, str]]:
    """Поиск по открытому API Rutube: [(страница ролика, название), ...].

    Взрослое (`is_adult`) и прямые эфиры отбрасываем: робот ищет мультики
    для дома, а эфир ни на паузу не ставится, ни «следующим» не листается.
    """
    адрес = RUTUBE_ПОИСК + "?" + urllib.parse.urlencode(
        {"query": что, "format": "json"})
    данные = _читать_json(адрес)
    результат: list[tuple[str, str]] = []
    for запись in данные.get("results") or []:
        if not isinstance(запись, dict):
            continue
        страница = запись.get("video_url")
        if not страница or запись.get("is_adult") or запись.get("is_livestream"):
            continue
        результат.append((str(страница), str(запись.get("title") or "").strip()))
        if len(результат) >= сколько:
            break
    return результат


def прямой_адрес(страница: str) -> str:
    """HLS-адрес ролика Rutube для mpv — или сама страница, если не вышло.

    mpv умеет играть m3u8 сам, без yt-dlp, — а страницу ролика без yt-dlp не
    разберёт. Поэтому адрес разрешаем здесь, одним запросом к
    `api/play/options`, ровно перед запуском. Не вышло — отдаём страницу:
    mpv попробует через свой ytdl_hook, если yt-dlp у него под рукой.
    """
    номер = _номер_ролика(страница)
    if not номер:
        return страница
    try:
        данные = _читать_json(RUTUBE_ВОСПРОИЗВЕДЕНИЕ.format(id=номер) + "?format=json")
    except Недоступен as e:
        log.warning("видео: прямой адрес не получен (%s) — отдаю mpv страницу", e)
        return страница
    балансир = данные.get("video_balancer") or {}
    m3u8 = балансир.get("m3u8") or next(
        (а for а in балансир.values() if isinstance(а, str) and ".m3u8" in а), "")
    if not m3u8:
        log.warning("видео: в ответе Rutube нет m3u8 (%s) — отдаю mpv страницу",
                    str(данные)[:120])
        return страница
    return str(m3u8)


def _номер_ролика(страница: str) -> str:
    """32 шестнадцатеричных знака из https://rutube.ru/video/<id>/."""
    куски = [к for к in urllib.parse.urlparse(страница).path.split("/") if к]
    for к in reversed(куски):
        if len(к) == 32 and all(с in "0123456789abcdef" for с in к.lower()):
            return к
    return ""


def _искать_youtube(что: str, сколько: int) -> list[tuple[str, str]]:
    """YouTube через yt-dlp — прежний путь, за флагом ROBOT_VIDEO_SOURCE."""
    try:
        import yt_dlp
    except ImportError:
        log.warning("видео: yt-dlp не установлен")
        return []
    опции = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "socket_timeout": ТАЙМАУТ,
        # Повторов нет — см. ТАЙМАУТ. Все три: yt-dlp повторяет и запрос, и
        # разбор, и куски порознь.
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
    }

    def искать():
        with yt_dlp.YoutubeDL(опции) as ydl:
            return ydl.extract_info(f"ytsearch{сколько}:{что}", download=False)

    # Срок на весь поиск. Исполнитель не ждём при выходе (shutdown без
    # wait): просроченный поток остаётся доделывать своё в фоне, а мы уже
    # отвечаем человеку.
    исполнитель = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        данные = исполнитель.submit(искать).result(timeout=ТАЙМАУТ_ВСЕГО)
    except concurrent.futures.TimeoutError:
        log.warning("видео: YouTube не ответил за %.0f с — поиск «%s» брошен",
                    ТАЙМАУТ_ВСЕГО, что)
        raise Недоступен(f"YouTube не ответил за {ТАЙМАУТ_ВСЕГО:.0f} с") from None
    except Exception as e:                       # noqa: BLE001
        log.warning("видео: поиск «%s» не вышел (%s)", что, e)
        # Сетевая беда и «ничего не нашлось» — разные ответы человеку. Всё,
        # что yt-dlp кидает при недоступной сети, содержит эти слова; на
        # прочее честно отвечаем «не нашёл».
        текст = str(e).lower()
        if any(с in текст for с in ("timed out", "timeout", "connection",
                                    "unable to download", "name resolution")):
            raise Недоступен(str(e)[:120]) from None
        return []
    finally:
        исполнитель.shutdown(wait=False)
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


def search(что: str, сколько: int = 8) -> list[tuple[str, str]]:
    """Первые «сколько» роликов по запросу: [(адрес, название), ...].

    Пустой список — не нашёл (или yt-dlp не установлен на пути YouTube).
    Сеть не ответила — Недоступен: это другой ответ человеку.
    """
    что = " ".join((что or "").split())
    if not что:
        return []
    сколько = max(1, min(20, int(сколько)))
    if ИСТОЧНИК == "youtube":
        return _искать_youtube(что, сколько)
    return _искать_rutube(что, сколько)
