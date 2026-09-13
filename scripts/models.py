#!/usr/bin/env python3
"""Список моделей у посредника — какие есть, что умеют, почём.

Зачем отдельный скрипт. Выбор модели упирается в три числа: берёт ли она
картинку, сколько стоит вход и сколько выход. На сайте это разбросано по
карточкам с фильтрами, а решать приходится по цифрам — особенно для зрения,
где картинка весит полторы тысячи токенов и цена входа решает всё.

Ключ и адрес берутся из тех же настроек, что у робота, и на экран не
попадают: ключ уходит только в заголовок запроса.

    python3 scripts/models.py            # зрячие, от дешёвых к дорогим
    python3 scripts/models.py --все      # вообще все
    python3 scripts/models.py --сырое    # как ответил посредник, без разбора

Последнее пригодится, если посредник отвечает не так, как мы ждём: разбор
рассчитан на openai-подобный ответ, но у каждого свои поля, и гадать о них
хуже, чем посмотреть.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

НАСТРОЙКИ = Path.home() / ".robot-ai.env"


def из_настроек() -> dict[str, str]:
    """Прочитать ~/.robot-ai.env, не запуская его как скрипт."""
    добыто: dict[str, str] = {}
    if not НАСТРОЙКИ.exists():
        return добыто
    for строка in НАСТРОЙКИ.read_text(encoding="utf-8").splitlines():
        строка = строка.strip()
        if not строка or строка.startswith("#") or "=" not in строка:
            continue
        имя, _, значение = строка.partition("=")
        добыто[имя.strip()] = значение.strip().strip('"').strip("'")
    return добыто


def адрес_и_ключ() -> tuple[str, str]:
    настройки = из_настроек()

    def взять(имя: str) -> str:
        return os.environ.get(имя) or настройки.get(имя, "")

    база = взять("ROBOT_API_BASE").rstrip("/")
    ключ = взять("ANTHROPIC_API_KEY")
    if not база:
        # Пусто — значит робот ходит напрямую в Anthropic, а не к посреднику.
        база = "https://api.anthropic.com/v1"
    if not база.endswith("/v1"):
        база += "/v1"
    return база, ключ


def достать(url: str, ключ: str) -> dict:
    запрос = urllib.request.Request(url, headers={
        # Два заголовка сразу: какой протокол ждёт посредник — заранее не
        # известно, а лишний он просто не заметит.
        "Authorization": f"Bearer {ключ}",
        "x-api-key": ключ,
        "anthropic-version": "2023-06-01",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(запрос, timeout=30) as ответ:
        return json.loads(ответ.read().decode("utf-8"))


def цена(модель: dict, что: str) -> float | None:
    """Цена за миллион токенов, где бы посредник её ни спрятал."""
    расценки = модель.get("pricing") or модель.get("цены") or {}
    if not isinstance(расценки, dict):
        return None
    ключи = ({"prompt", "input", "input_tokens", "вход"} if что == "вход"
             else {"completion", "output", "output_tokens", "выход"})
    for имя, значение in расценки.items():
        if имя.lower() not in ключи:
            continue
        try:
            число = float(значение)
        except (TypeError, ValueError):
            continue
        # Одни отдают цену за токен, другие — за миллион. Отличаем по
        # величине, и граница тут — единица, а не какая-нибудь тысячная.
        #
        # Порог 0.001 казался «исчезающе малым» и был неверен: 1105 ₽ за
        # миллион — это 0.001105 за токен, то есть БОЛЬШЕ порога. Цена честно
        # оставалась потокенной и печаталась как «0.00». Вход при этом
        # переводился правильно, и таблица выглядела так, будто выход у всех
        # бесплатный.
        #
        # Единица безопасна с обеих сторон: цена в рублях за миллион ниже
        # рубля не встречается, а рубль за один токен — это миллион за
        # миллион, чего не бывает.
        return число * 1_000_000 if число < 1.0 else число
    return None


def видит(модель: dict) -> bool | None:
    """Принимает ли модель картинку. None — посредник не сказал.

    Три состояния, а не два, и это важно. Раньше при отсутствии поля мы
    искали слово «image» во всём описании разом — и получали «да» вообще у
    всех, включая генератор музыки. Столбец, который всегда говорит «да», не
    отвечает ни на что, но выглядит как ответ; лучше честный вопросительный
    знак.
    """
    for где in (модель.get("architecture") or {}, модель):
        входы = где.get("input_modalities") or где.get("input")
        if isinstance(входы, list):
            return any("image" in str(x).lower() or "изображ" in str(x).lower()
                       for x in входы)
    return None


def выдаёт_текст(модель: dict) -> bool:
    """Отсеиваем генераторы видео и картинок — нам нужен текст."""
    for где in (модель.get("architecture") or {}, модель):
        выходы = где.get("output_modalities") or где.get("output")
        if isinstance(выходы, list):
            return any("text" in str(x).lower() or "текст" in str(x).lower()
                       for x in выходы)
    return True         # не сказано — считаем, что текст


def главное() -> int:
    все = "--все" in sys.argv or "--all" in sys.argv
    сырое = "--сырое" in sys.argv or "--raw" in sys.argv
    база, ключ = адрес_и_ключ()
    if not ключ:
        print("Нет ключа: ни ANTHROPIC_API_KEY в окружении, ни в "
              f"{НАСТРОЙКИ}", file=sys.stderr)
        return 1

    url = база + "/models"
    print(f"спрашиваю {url}", file=sys.stderr)
    try:
        ответ = достать(url, ключ)
    except urllib.error.HTTPError as e:
        тело = e.read().decode("utf-8", "replace")[:400]
        print(f"посредник отказал ({e.code}): {тело}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"не дозвонился: {e}", file=sys.stderr)
        return 1

    if сырое:
        print(json.dumps(ответ, ensure_ascii=False, indent=2))
        return 0

    модели = ответ.get("data") or ответ.get("models") or ответ
    if not isinstance(модели, list):
        print("Ответ не похож на список моделей — вот он целиком:",
              file=sys.stderr)
        print(json.dumps(ответ, ensure_ascii=False, indent=2)[:3000])
        return 1

    # Неизвестное (None) не выбрасываем: лучше показать со знаком вопроса,
    # чем молча спрятать то, что могло подойти.
    отобрано = [м for м in модели if isinstance(м, dict)
                and (все or (видит(м) is not False and выдаёт_текст(м)))]
    # Без цены — в конец: это не повод прятать модель, но и не повод
    # показывать её выше тех, про которые всё известно.
    отобрано.sort(key=lambda м: (цена(м, "вход") is None,
                                 цена(м, "вход") or 0.0))

    print(f"{'модель':52} {'вход/1М':>10} {'выход/1М':>10}  зрение")
    print("-" * 88)
    for м in отобрано:
        имя = str(м.get("id") or м.get("name") or "?")[:52]
        вх, вых = цена(м, "вход"), цена(м, "выход")
        зрение = {True: "да", False: "—", None: "?"}[видит(м)]
        print(f"{имя:52} "
              f"{('%.2f' % вх) if вх is not None else '—':>10} "
              f"{('%.2f' % вых) if вых is not None else '—':>10}"
              f"  {зрение}")
    print(f"\nвсего {len(отобрано)} из {len(модели)}", file=sys.stderr)
    if any(видит(м) is None for м in отобрано):
        print("Знак вопроса — посредник не сказал, берёт ли модель картинку. "
              "Проверить точно: python3 scripts/models.py --сырое",
              file=sys.stderr)
    if отобрано and цена(отобрано[0], "вход") is None:
        print("Цен в ответе нет — посредник их не отдаёт. Тогда смотреть на "
              "сайте, а отсюда брать точные имена.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(главное())
