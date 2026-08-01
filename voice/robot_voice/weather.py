"""Погода с open-meteo.com — без ключа, без регистрации, без денег.

Второй по частоте вопрос к домашнему ассистенту после таймеров, и робот на
него отвечать обязан: без инструмента модель погоду просто выдумает.

Координаты дома — в ROBOT_LAT и ROBOT_LON. Без них робот честно говорит, что
не знает, где он находится: врать про погоду в чужом городе хуже, чем молчать.

Разбор ответа отделён от запроса намеренно: сеть в тесте не поднять, а вот
формулировки проверить надо.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import ru

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 8

# Коды WMO. Словами, как их произносит человек, а не как в справочнике.
_SKY = {
    0: "ясно", 1: "почти ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман с изморозью",
    51: "морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "ливень", 81: "ливень", 82: "сильный ливень",
    85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}

# Когда стоит предупредить про одежду — вопрос всегда про это.
_WET = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _degrees(value: float) -> str:
    """«минус три градуса», «плюс двадцать один градус»."""
    n = int(round(value))
    word = ru.count(abs(n), "градус", "градуса", "градусов")
    if n < 0:
        return f"минус {word}"
    return word


def describe_now(data: dict) -> str:
    cur = data.get("current") or {}
    temp = cur.get("temperature_2m")
    if temp is None:
        return "Погода не отвечает, попробуй позже."
    code = int(cur.get("weather_code") or 0)
    sky = _SKY.get(code, "без осадков")
    phrase = f"Сейчас на улице {_degrees(temp)}, {sky}"

    wind = cur.get("wind_speed_10m")
    if wind and wind >= 8:
        phrase += f", ветер {ru.count(int(round(wind)), 'метр', 'метра', 'метров')} в секунду"
    if code in _WET:
        phrase += ". Зонт пригодится"
    return phrase + "."


def describe_tomorrow(data: dict) -> str:
    daily = data.get("daily") or {}
    highs, lows = daily.get("temperature_2m_max") or [], daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    # Нулевой день — сегодня, первый — завтра.
    if len(highs) < 2 or len(lows) < 2:
        return "Прогноза на завтра не пришло."
    code = int(codes[1]) if len(codes) > 1 else 0
    sky = _SKY.get(code, "без осадков")
    # «от одиннадцати до восемнадцати» требовало бы родительного падежа —
    # проще сказать так, как говорят в прогнозе по радио.
    phrase = f"Завтра днём {_degrees(highs[1])}, ночью {_degrees(lows[1])}, {sky}"
    if code in _WET:
        phrase += ". Возьми зонт"
    return phrase + "."


def fetch(lat: float, lon: float) -> dict | None:
    """Ходит в open-meteo. None — не дозвонились, и это нормально."""
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 2,
    })
    try:
        with urllib.request.urlopen(f"{URL}?{query}", timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("погода не ответила: %s", e)
        return None
