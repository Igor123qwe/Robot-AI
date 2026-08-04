"""Инструменты, которые модель может вызывать: движение, батарея, таймеры.

Схемы описаны руками, а не выведены из сигнатур. Причина простая: робот ходит
не только к api.anthropic.com, но и через сторонний роутер, а тот понимает
обычный /v1/messages и не обязан поддерживать beta-эндпоинт с его
автогенерацией. Явная схема работает везде одинаково.

Описания в схемах пишутся для модели, а не для человека — по ним она решает,
что и когда вызывать.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import counting, ru, when
from . import weather as weather_api

log = logging.getLogger(__name__)

# Как зовётся таймер, которому названия не дали.
NO_NAME = "без названия"
ALARM = "будильник"

# Сколько раз повторять объявление, если его никто не слышал, и с каким шагом.
RING_TRIES = 3
RING_RETRY = 45.0

# Насколько просроченный таймер ещё стоит объявлять после перезапуска.
# Полчаса — это «робот моргнул, пока ты выходил на кухню». Всё, что старше,
# уже не новость, а испуг: робот стоял неделю и вдруг объявляет будильник
# недельной давности.
STALE_SECONDS = 1800.0


def _ring(label: str) -> str:
    name = "Таймер" if label == NO_NAME else f"Таймер {label}"
    return f"{name} — время вышло."


def _wake_up(target) -> str:
    """Чем робот будит. Днём это странно звучало бы как «доброе утро»."""
    greeting = "Доброе утро" if 4 <= target.hour < 11 else "Подъём"
    return f"{greeting}, {ru.clock(target)}."


def _short(text: str, limit: int = 30) -> str:
    """Обрезает по границе слова: имя таймера робот читает вслух."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] or text[:limit]


def _key(label: str) -> str:
    """Имя таймера для сравнения: регистр и ё/е не должны мешать."""
    return " ".join(label.lower().replace("ё", "е").split())


def _free_label(base: str, taken: dict) -> str:
    label = base
    keys = {_key(name) for name in taken}
    while _key(label) in keys:
        label += " ещё"
    return label


# Ответ на «что ты умеешь». Текст заранее известен, поэтому спрашивать о нём
# модель — платить за то, что и так написано. Держать в синхроне с набором
# инструментов ниже: человек проверит первым делом именно это.
ABILITIES = (
    "Я умею ездить по квартире и разворачиваться, могу остановиться по слову "
    "стоп. Скажу, сколько заряда в батарее, который час и какое сегодня число. "
    "Ставлю таймеры и будильники, напоминаю о делах, веду список покупок. "
    "Расскажу погоду, новости и курс валют, скажу, который час в другом "
    "городе, и переведу метры в футы или доллары в рубли. "
    "Включу музыку — по исполнителю, "
    "по жанру или просто твою волну. Громкость меняю по десятибалльной: "
    "скажи «сделай на семь». Есть секундомер, считаю проценты и деление, "
    "брошу монетку или кубик и загадаю число. "
    "Всё остальное — просто спроси, я отвечу."
)

# Скорости для голосовых команд — заведомо ниже предела пульта.
DRIVE_SPEED = 0.20   # м/с
TURN_SPEED = 1.00    # рад/с

MAX_DISTANCE = 3.0   # м за одну команду
MAX_ANGLE = 360.0    # градусов

# Ниже этого напряжения не едем вообще: 3S li-ion уже на грани.
CUTOFF_VOLT = 10.2

# Разрядная кривая 3S li-ion — та же, что в веб-пульте.
_CURVE = [
    (12.60, 100), (12.30, 90), (12.00, 75), (11.70, 60),
    (11.40, 50), (11.10, 45), (10.80, 25), (10.20, 8), (9.90, 0),
]

_DIRECTIONS = {
    "вперёд": (1.0, 0.0), "вперед": (1.0, 0.0), "forward": (1.0, 0.0),
    "назад": (-1.0, 0.0), "back": (-1.0, 0.0),
    "влево": (0.0, 1.0), "left": (0.0, 1.0),
    "вправо": (0.0, -1.0), "right": (0.0, -1.0),
}

EMPTY_SCHEMA = {"type": "object", "properties": {}}

# Имена полей в схеме API принимает только латиницей: ^[a-zA-Z0-9_.-]{1,64}$.
# Наши инструменты объявлены по-русски, как и весь остальной код, и один такой
# ключ отклоняет ВЕСЬ список инструментов разом — то есть любой запрос к
# модели, а не только этот инструмент. В журнале это выглядит как
# «tools.5.custom.input_schema.properties», и по номеру инструмента ещё надо
# догадаться. Робот при этом слышит и всё понимает, но ответить не может ничем.
#
# Поэтому переводим только на границе с API: наружу уходит латиница, обратно
# приходит она же и превращается в наши имена. Ни правила, ни сами функции об
# этом не знают — им по-прежнему приходит русское.
_ПО_АНГЛИЙСКИ = {
    "что": "query", "изменение": "change", "уровень": "level", "шаг": "step",
    "вид": "kind", "снизу": "low", "сверху": "high", "сколько": "amount",
    "из_чего": "from_unit", "во_что": "to_unit", "город": "city",
    "варианты": "options",
    "выражение": "expression", "вопрос": "question", "факт": "fact",
    "действие": "action", "источник": "source",
}
# Запасная транслитерация — на случай, если кто-то заведёт новое русское имя и
# забудет вписать его выше. Имя выйдет некрасивым, зато робот не онемеет.
_БУКВЫ = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ы": "y", "э": "e",
    "ю": "yu", "я": "ya", "ъ": "", "ь": "", "ё": "e",
}


def _годная(буква: str) -> bool:
    """Такую букву API в имени поля примет."""
    return буква.isascii() and (буква.isalnum() or буква in "_.-")


def _латиницей(имя: str) -> str:
    """Имя поля, каким его примет API."""
    готовое = _ПО_АНГЛИЙСКИ.get(имя)
    if готовое:
        return готовое
    вышло = "".join(_БУКВЫ.get(б, б if _годная(б) else "_") for б in имя.lower())
    return (вышло or "arg")[:64]


def _наружу(схема: dict) -> dict:
    """Схема для API: имена полей латиницей, всё остальное как было."""
    свойства = схема.get("properties") or {}
    if all(_годная(б) for имя in свойства for б in имя):
        return схема
    новая = dict(схема)
    новая["properties"] = {_латиницей(и): з for и, з in свойства.items()}
    if схема.get("required"):
        новая["required"] = [_латиницей(и) for и in схема["required"]]
    return новая


def _внутрь(схема: dict, args: dict) -> dict:
    """То, что вернула модель, — обратно именами этого инструмента.

    Смотрим на схему самого инструмента, а не в общий список: у «volume» поле
    и так называется change по-английски, и общая замена превращала его в
    «изменение» — то есть чинила одно и ломала соседнее.
    """
    свои = {_латиницей(и): и for и in (схема.get("properties") or {})}
    return {свои.get(и, и): з for и, з in (args or {}).items()}


@dataclass
class Tool:
    """Инструмент: описание для модели плюс то, что реально выполняется."""

    name: str
    description: str
    input_schema: dict
    run: Callable[..., str]
    # Инструменты, которые надёжно разбираются правилами, модели не показываем:
    # каждая схема едет в КАЖДОМ запросе и оплачивается каждый раз. Вызвать
    # такой инструмент модель не сможет, но ей это и не нужно — до неё эти
    # фразы просто не доходят.
    hidden: bool = False

    def spec(self) -> dict:
        """То, что уходит в API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _наружу(self.input_schema),
        }

    def __call__(self, args: dict[str, Any]) -> str:
        try:
            return self.run(**_внутрь(self.input_schema, args))
        except TypeError as e:
            # Модель придумала лишний или пропустила обязательный аргумент.
            log.warning("неверные аргументы для %s: %s", self.name, e)
            return f"Не понял, как вызвать {self.name}: {e}"
        except Exception as e:
            log.exception("инструмент %s упал", self.name)
            return f"Инструмент {self.name} не сработал: {e}"


def _say_distance(metres: float) -> str:
    """Расстояние словами: «полметра» звучит естественнее, чем «0.50 м»."""
    if abs(metres - 0.5) < 0.02:
        return "полметра"
    if abs(metres - 1.0) < 0.02:
        return "метр"
    if abs(metres - 1.5) < 0.02:
        return "полтора метра"
    if metres < 1.0:
        cm = int(round(metres * 100))
        return "на " + ru.count(cm, "сантиметр", "сантиметра", "сантиметров")
    whole = int(metres)
    half = round(metres - whole, 2)
    if half >= 0.45:
        return f"на {ru.cardinal(whole)} с половиной метра"
    return "на " + ru.count(whole, "метр", "метра", "метров")


def _say_percent(pct: float) -> str:
    return ru.count(int(round(pct)), "процент", "процента", "процентов")


def volt_to_percent(v: float) -> float:
    if v >= _CURVE[0][0]:
        return 100.0
    if v <= _CURVE[-1][0]:
        return 0.0
    for (v_hi, p_hi), (v_lo, p_lo) in zip(_CURVE, _CURVE[1:]):
        if v_lo <= v <= v_hi:
            k = (v - v_lo) / (v_hi - v_lo)
            return p_lo + k * (p_hi - p_lo)
    return 0.0   # страховка: сюда попадём, только если в _CURVE появится дыра


class Timers:
    """Таймеры, которые робот озвучивает вслух, когда они срабатывают.

    Хранятся на диске: автообновление перезапускает сервис каждые две минуты,
    если в репозитории что-то поменялось, и таймер на духовку не должен от
    этого пропадать. Сроки записываем в обычном времени (time.time), а не в
    monotonic — оно не переживает перезапуск.
    """

    def __init__(self, announce: Callable[..., None],
                 store: Path | None = None) -> None:
        self._announce = announce
        self._items: dict[str, tuple[threading.Timer, float]] = {}
        # Снятые с паузы: имя → сколько секунд оставалось.
        self._paused: dict[str, float] = {}
        # Что сказать, когда сработает. Пусто — обычное «время вышло»;
        # у будильника и напоминания фраза своя.
        self._messages: dict[str, str] = {}
        # Сколько раз таймер уже звонил в пустоту.
        self._tries: dict[str, int] = {}
        # Рекурсивный: SIGTERM приходит в главный поток, и обработчик зовёт
        # cancel_all() ровно тогда, когда этот же поток может держать замок.
        self._lock = threading.RLock()
        self.store = store

    def _known(self, label: str) -> str:
        """Уже заведённое имя с тем же ключом, иначе само label.

        Хранились имена точной строкой, а искались нестрого — с приведением
        регистра и ё к е. Из-за расхождения «Лапша» и «лапша» становились
        двумя разными таймерами, но одним и тем же для поиска: робот отменял
        первый попавшийся, бодро отвечал «отменил», а второй продолжал идти.
        Приводим к одному имени здесь, в хранилище, а не в каждом месте, где
        таймер ищут.
        """
        key = _key(label)
        for name in list(self._items) + list(self._paused):
            if _key(name) == key:
                return name
        return label

    def add(self, label: str, seconds: float, message: str = "") -> None:
        with self._lock:
            label = self._known(label)
            self.cancel(label, _locked=True)
            timer = threading.Timer(seconds, self._fire, args=(label,))
            timer.daemon = True
            self._items[label] = (timer, time.monotonic() + seconds)
            if message:
                self._messages[label] = message
            timer.start()
            self._save()

    def cancel(self, label: str, *, _locked: bool = False) -> bool:
        if not _locked:
            self._lock.acquire()
        try:
            label = self._known(label)
            paused = self._paused.pop(label, None)
            item = self._items.pop(label, None)
            if item is not None:
                item[0].cancel()
            if item is not None or paused is not None:
                self._messages.pop(label, None)
                self._tries.pop(label, None)
                self._save()
            return item is not None or paused is not None
        finally:
            if not _locked:
                self._lock.release()

    def pause(self, label: str) -> bool:
        """Останавливает отсчёт, запомнив остаток."""
        with self._lock:
            item = self._items.pop(self._known(label), None)
            if item is None:
                return False
            timer, due = item
            timer.cancel()
            self._paused[label] = max(0.0, due - time.monotonic())
            self._save()
            return True

    def resume(self, label: str) -> bool:
        with self._lock:
            label = self._known(label)
            left = self._paused.pop(label, None)
            message = self._messages.get(label, "")
        if left is None:
            return False
        # add берёт тот же замок, поэтому зовём его уже снаружи.
        self.add(label, left, message)
        return True

    def remaining(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            return {label: max(0.0, due - now) for label, (_, due) in self._items.items()}

    def paused(self) -> dict[str, float]:
        with self._lock:
            return dict(self._paused)

    def messages(self) -> dict[str, str]:
        """Кому что сказать при срабатывании. Пусто — обычный таймер."""
        with self._lock:
            return dict(self._messages)

    def cancel_all(self) -> None:
        """Человек попросил снять все таймеры. С диска — тоже."""
        with self._lock:
            for timer, _ in self._items.values():
                timer.cancel()
            self._items.clear()
            self._paused.clear()
            self._messages.clear()
            self._tries.clear()
            self._save()

    def stop(self) -> None:
        """Робот выключается. Таймеры при этом снимать НЕЛЬЗЯ.

        Разница с cancel_all принципиальная, и раньше её не было: при
        остановке звался cancel_all, а он вместе с потоками стирал и файл.
        Автообновление перезапускает сервис при каждой правке, то есть
        хранилище обнулялось по нескольку раз в день — и таймер на духовку
        пропадал ровно в том случае, ради которого файл и заведён.

        Гасить потоки строго говоря незачем, они daemon и умрут сами. Но
        сделать это явно дешевле, чем однажды заговорить на полуслове.
        """
        with self._lock:
            for timer, _ in self._items.values():
                timer.cancel()

    def _fire(self, label: str) -> None:
        with self._lock:
            self._items.pop(label, None)
            message = self._messages.get(label, "")
            tries = self._tries.get(label, 1)
            # Отметка «этот таймер сейчас звонит». По ней после объявления
            # видно, не отменили ли его, пока замок был отпущен: cancel и
            # cancel_all её снимают.
            self._tries[label] = tries
            # На диске таймер пока остаётся: если сервис перезапустят прямо
            # сейчас (автообновление раз в две минуты), напоминание не должно
            # пропасть — restore() увидит просроченный и произнесёт его.

        # Таймер, будильник и напоминание человек ставил сам — они звучат в
        # полную громкость даже в тихие часы. Иначе тихий режим превращает
        # будильник в бесполезный.
        heard = self._announce(message or _ring(label), loud=True)

        # Своего динамика нет: реплику играет вкладка пульта, а она бывает
        # закрыта или с выключенным звуком. Прозвонить в пустоту и забыть —
        # это потерянный таймер на духовке, поэтому повторяем.
        if heard is not None and heard <= 0 and tries < RING_TRIES:
            with self._lock:
                # Объявление длится секунды, и замок на это время отпущен.
                # За это время человек мог сказать «отмени все» — тогда
                # повторять нечего. Раньше повтор просто создавал таймер
                # заново, и отменённый будильник воскресал, да ещё и
                # записывался обратно на диск.
                if label not in self._tries:
                    log.info("таймер %r отменили, пока он звонил", label)
                    return
                log.info("таймер %r никто не услышал, повторю через %.0f с",
                         label, RING_RETRY)
                # Счёт попыток ставим ПОСЛЕ add: он внутри зовёт cancel, а тот
                # отметку снимает. Раньше строки шли наоборот, счётчик
                # обнулялся на каждом круге, и таймер в закрытую вкладку звонил
                # бы вечно вместо трёх раз.
                self.add(label, RING_RETRY, message)
                self._tries[label] = tries + 1
            return

        with self._lock:
            self._tries.pop(label, None)
            self._messages.pop(label, None)
            self._save()

    # --- переживание перезапуска ----------------------------------------
    def _save(self) -> None:
        """Пишет состояние на диск. Замок уже держит вызывающий."""
        if self.store is None:
            return
        shift = time.time() - time.monotonic()
        data = {
            "items": {label: due + shift for label, (_, due) in self._items.items()},
            "paused": dict(self._paused),
            "messages": dict(self._messages),
        }
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_name(self.store.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(self.store)      # замена целиком, без полуфайлов
        except OSError as e:
            log.warning("не смог сохранить таймеры: %s", e)

    def restore(self) -> None:
        """Поднимает таймеры, пережившие перезапуск сервиса."""
        if self.store is None or not self.store.exists():
            return
        try:
            data = json.loads(self.store.read_text())
        except (OSError, ValueError) as e:
            log.warning("не смог прочитать сохранённые таймеры: %s", e)
            return

        now = time.time()
        messages = data.get("messages") or {}
        late = []
        for label, due in (data.get("items") or {}).items():
            left = due - now
            if left > 1:
                self.add(label, left, messages.get(label, ""))
                log.info("восстановил таймер %r, осталось %.0f с", label, left)
            elif -left <= STALE_SECONDS:
                late.append(label)
            else:
                # Слишком старое. Робот стоял неделю, а по возвращении
                # объявлял недельной давности будильник — это не забота, это
                # пугает. Плюс часы на этой плате врут при старте: сервис
                # поднимается раньше, чем NTP их поправит, и «просроченным»
                # может показаться таймер, который на самом деле идёт.
                log.info("таймер %r просрочен на %.0f ч — молчу",
                         label, -left / 3600)
        with self._lock:
            self._paused.update(data.get("paused") or {})
            # Текст напоминания, стоящего на паузе, тоже надо вернуть: иначе
            # после продолжения робот скажет безликое «время вышло».
            self._messages.update({k: v for k, v in messages.items()
                                   if k in self._paused or k in self._items})
            self._save()
        for label in late:
            # Сработал, пока сервиса не было. Молчать нельзя: человек ждал.
            said = messages.get(label) or _ring(label)
            self._announce(f"Пока меня не было: {said[0].lower()}{said[1:]}")


def build_tools(ros, timers: Timers, *, speaker=None, notes=None,
                place: tuple[float, float] | None = None,
                addressed: Callable[[], bool] | None = None,
                people=None, who: Callable[[], str] | None = None,
                home: Callable[[], tuple[float, float] | None] | None = None,
                set_place: Callable[[str, float, float], None] | None = None,
                player=None,
                news_url: str = "") -> list[Tool]:
    """Собирает набор инструментов, привязанный к конкретному роботу.

    speaker нужен для громкости и «повтори», notes — для списка покупок,
    place — координаты дома для погоды, addressed отвечает, звали ли робота
    по имени в текущей реплике. Всё необязательно: без этого соответствующие
    инструменты не появятся, и модель о них не узнает.
    """
    секундомер = counting.Секундомер()

    def name_guard() -> str | None:
        """Ехать — только по надёжной просьбе, кто бы ни просил.

        Проверка стоит здесь, а не в правилах, намеренно: «поезжай на кухню»
        правилами не разбирается и уходит модели, а именно такие формулировки
        и звучат из телевизора. Раньше страховка ловила только однозначное
        «вперёд» и пропускала всё остальное.

        Условий два: робота позвали по имени И фразу разобрали уверенно.
        Второе добавлено после того, как на живом роботе whisper услышал
        «Кузяка идла», модель домыслила из этого «влево», и робот поехал —
        имя совпало, и первого условия не хватило.
        """
        if addressed is None or addressed():
            return None
        if getattr(addressed, "by_name", True):
            log.info("расслышал неуверенно — не поеду")
            return "Не уверен, что расслышал. Повтори, пожалуйста."
        log.info("движение без имени — не поеду")
        return "Для поездки позови меня по имени."

    def battery_guard() -> str | None:
        # Без связи с шасси команда просто утонет: rosbridge молча выбрасывает
        # публикацию. Раньше робот в этом случае бодро отвечал «Еду вперёд» и
        # не ехал — час подряд, пока не заметишь.
        if not getattr(ros, "connected", True):
            return "Не могу: нет связи с шасси. Проверь, включён ли робот."
        volt = ros.voltage
        if volt is None:
            return None  # телеметрия ещё не пришла — не блокируем
        if volt < CUTOFF_VOLT:
            return (f"Не поеду: батарея {ru.volts(volt)}, "
                    f"это {_say_percent(volt_to_percent(volt))}. Нужна зарядка.")
        return None

    def drive(direction: str, distance: float = 0.5) -> str:
        key = direction.strip().lower()
        if key not in _DIRECTIONS:
            return f"Не понял направление {direction!r}. Можно: вперёд, назад, влево, вправо."

        blocked = name_guard() or battery_guard()
        if blocked:
            return blocked

        distance = max(0.05, min(MAX_DISTANCE, float(distance)))
        fx, fy = _DIRECTIONS[key]
        duration = distance / DRIVE_SPEED
        log.info("еду %s на %.2f м (%.1f с)", key, distance, duration)
        # Не блокируем: пока едем, робот должен слышать «стоп».
        ros.drive(fx * DRIVE_SPEED, fy * DRIVE_SPEED, 0.0, duration)
        return f"Еду {key} {_say_distance(distance)}."

    def turn(direction: str, degrees: float = 90.0) -> str:
        key = direction.strip().lower()
        if key in ("влево", "left"):
            sign = 1.0
        elif key in ("вправо", "right"):
            sign = -1.0
        else:
            return f"Не понял сторону {direction!r}. Можно: влево или вправо."

        blocked = name_guard() or battery_guard()
        if blocked:
            return blocked

        degrees = max(5.0, min(MAX_ANGLE, abs(float(degrees))))
        duration = (degrees * 3.14159265 / 180.0) / TURN_SPEED
        log.info("разворачиваюсь %s на %.0f° (%.1f с)", key, degrees, duration)
        ros.drive(0.0, 0.0, sign * TURN_SPEED, duration)
        turn_words = ru.count(int(round(degrees)), "градус", "градуса", "градусов")
        return f"Разворачиваюсь {key} на {turn_words}."

    def stop() -> str:
        # moving — это «едем ли по СВОЕЙ команде». Когда робота гонят
        # джойстиком с пульта, оно ложно, и робот отвечал «я и так стою»,
        # продолжая ехать: наши три нуля тут же перебивал поток пульта,
        # идущий пятнадцать раз в секунду. Врать в такой момент нельзя.
        was_moving = ros.moving
        was_busy = getattr(ros, "busy", was_moving)
        ros.stop_motion()
        log.info("стоп")
        if was_moving:
            return "Остановился."
        if was_busy:
            return "Меня ведут с пульта — отпусти джойстик, сам я не встану."
        return "Я и так стою."

    def battery() -> str:
        volt = ros.voltage
        if volt is None:
            return "Телеметрия батареи пока не пришла — шасси не отвечает."
        note = ""
        if volt < CUTOFF_VOLT:
            note = " Ехать уже нельзя, нужна зарядка."
        elif volt < 10.8:
            note = " Пора на зарядку."
        return (f"Батарея {ru.volts(volt)}, это примерно "
                f"{_say_percent(volt_to_percent(volt))}.{note}")

    def _named(label: str) -> str:
        """«таймер лапша», «напоминание позвонить маме», «будильник»."""
        if label == NO_NAME:
            return "таймер"
        if label.startswith(ALARM):
            return label            # «будильник», «будильник ещё»
        kind = "напоминание" if label in timers.messages() else "таймер"
        return f"{kind} {label}"

    def _the_only(label: str, pool: dict[str, float]) -> str | None:
        """Имя таймера, о котором речь. None — непонятно, надо переспросить.

        Сравниваем нестрого: робот сам зачитывает название вслух, человек
        повторяет его как услышал, а Whisper пишет то «Лапша», то «лапша»,
        то «ёжик» через «е». При точном сравнении переспрос зацикливался:
        робот спрашивал одно и то же на каждый ответ.

        Если таймер всего один — гадать вообще не о чем.
        """
        key = _key(label)
        for name in pool:
            if _key(name) == key:
                return name
        return next(iter(pool)) if len(pool) == 1 else None

    def set_timer(minutes: float, label: str = NO_NAME) -> str:
        # Ноль и минус — это не «шесть секунд», а недопонятая просьба. Раньше
        # «поставь таймер» без числа превращалось в таймер на шесть секунд,
        # который звонил раньше, чем человек успевал договорить.
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = 0.0
        if minutes <= 0:
            return "На сколько ставить таймер?"
        if minutes > 600.0:
            return (f"Дольше десяти часов таймер не ставлю. "
                    f"{ru.duration(600 * 60, accusative=True).capitalize()} — "
                    f"это предел.")
        minutes = max(0.1, minutes)
        label = label.strip() or NO_NAME
        extra = False
        # Имена, которые робот раздаёт сам, повторяются — и add() заменяет по
        # имени. Безымянный таймер раньше молча затирал предыдущий безымянный,
        # а «будильник через двадцать минут» — уже стоящий подъём на семь утра,
        # подтвердив вслух оба. Разводим имена, как это делает set_alarm.
        if label in (NO_NAME, ALARM):
            taken = {**timers.remaining(), **timers.paused()}
            if _key(label) in {_key(name) for name in taken}:
                base = ALARM if label == ALARM else ru.duration(minutes * 60)
                label = _free_label(base, taken)
                extra = True
        timers.add(label, minutes * 60)
        log.info("таймер %r на %.1f мин", label, minutes)
        how_long = ru.duration(minutes * 60, accusative=True)
        if extra:
            return f"Поставил ещё один таймер, на {how_long}."
        return f"Поставил {_named(label)} на {how_long}."

    def set_alarm(at: str, label: str = "") -> str:
        """Будильник на конкретное время: «в семь утра», «на 7:30»."""
        target = when.moment(at)
        if target is None:
            return f"Не понял время {at!r}. Скажи, например, в семь утра."
        minutes = when.minutes_until(target)
        # Имя по умолчанию у всех одно, а add() заменяет по имени: без этого
        # второй будильник молча стирал первый, подтвердив оба вслух.
        label = _free_label(label.strip() or ALARM,
                            {**timers.remaining(), **timers.paused()})
        timers.add(label, minutes * 60, message=_wake_up(target))
        log.info("будильник %r на %s (через %.0f мин)", label, target, minutes)
        return f"Разбужу {ru.when_phrase(target)}."

    def set_reminder(text: str, at: str = "", minutes: float = 0) -> str:
        """Напоминание: текст плюс когда — время суток или через сколько."""
        text = text.strip(" ,.!?")
        if not text:
            return "О чём напомнить?"
        if at.strip():
            target = when.moment(at)
            if target is None:
                return f"Не понял время {at!r}."
            minutes = when.minutes_until(target)
            said_when = ru.when_phrase(target)
        elif minutes:
            minutes = max(0.1, min(1440.0, float(minutes)))
            said_when = f"через {ru.duration(minutes * 60, accusative=True)}"
        else:
            return "Когда напомнить?"

        label = _free_label(_short(text), {**timers.remaining(), **timers.paused()})
        timers.add(label, minutes * 60, message=f"Напоминаю: {text}.")
        log.info("напоминание %r через %.1f мин", text, minutes)
        return f"Напомню {said_when}."

    def list_timers() -> str:
        left = timers.remaining()
        held = timers.paused()
        if not left and not held:
            return "Активных таймеров нет."
        parts = [f"{_named(label)} — осталось {ru.duration(sec)}"
                 for label, sec in left.items()]
        parts += [f"{_named(label)} на паузе, на нём {ru.duration(sec)}"
                  for label, sec in held.items()]
        listed = "; ".join(parts)
        return listed[0].upper() + listed[1:]

    def _which(label: str, pool: dict[str, float], question: str) -> str:
        """Переспрос, когда непонятно, о каком именно таймере речь."""
        # Сюда попадаем, только когда таймеров больше одного: единственный
        # разбирается в _the_only без вопросов.
        names = ["безымянный" if n == NO_NAME else n for n in pool]
        listed = ", ".join(names[:-1]) + " и " + names[-1]
        head = f"Таймера {label} нет. " if label else ""
        return f"{head}Есть {listed}. {question}"

    def cancel_timer(label: str = "") -> str:
        label = label.strip()
        pool = {**timers.remaining(), **timers.paused()}
        if not pool:
            return "Активных таймеров нет."
        target = _the_only(label, pool)
        if target is None:
            return _which(label, pool, "Какой снять?")
        timers.cancel(target)
        return f"Отменил {_named(target)}."

    def pause_timer(label: str = "") -> str:
        label = label.strip()
        running = timers.remaining()
        if not running:
            return "Идущих таймеров нет."
        target = _the_only(label, running)
        if target is None:
            return _which(label, running, "Какой поставить на паузу?")
        timers.pause(target)
        return f"Остановил {_named(target)}, на нём {ru.duration(running[target])}."

    def resume_timer(label: str = "") -> str:
        label = label.strip()
        held = timers.paused()
        if not held:
            return "На паузе ничего нет."
        target = _the_only(label, held)
        if target is None:
            return _which(label, held, "Какой продолжить?")
        timers.resume(target)
        return f"Продолжаю {_named(target)}, осталось {ru.duration(held[target])}."

    def cancel_all_timers(confirmed: bool = False) -> str:
        count = len(timers.remaining()) + len(timers.paused())
        if not count:
            return "Активных таймеров и так нет."
        if count > 1 and not confirmed:
            # Снести чужой таймер на духовку из-за неточно понятой фразы —
            # это то, что человек уже не восстановит. Один вопрос дешевле.
            how_many = ru.count(count, "таймер", "таймера", "таймеров")
            return f"{how_many.capitalize()}. Отменить все?"
        timers.cancel_all()
        word = ru.plural(count, "он был", "их было", "их было")
        return f"Отменил все таймеры, {word} {ru.cardinal(count)}."

    def time_now(город: str = "") -> str:
        """Который час. С городом — там; своё время робот берёт из своих часов.

        Который час в Москве, модель по памяти не знает вовсе: она выдумает
        смещение и не будет знать текущего момента. Часы есть только здесь.
        """
        город = (город or "").strip()
        if город:
            ответ = counting.время_в(город)
            if ответ:
                return ответ
            return (f"Не знаю, какой часовой пояс у города {город}. "
                    f"Здесь сейчас {ru.clock()}.")
        return f"Сейчас {ru.clock()}."

    def convert_units(сколько: float = 1, из_чего: str = "", во_что: str = "") -> str:
        try:
            число = float(сколько)
        except (TypeError, ValueError):
            return "Сколько переводить?"
        # Деньги считаем по сегодняшнему курсу, а не по таблице: в метре
        # сантиметров всегда сто, а доллар вчерашним не бывает.
        from . import lookup
        деньги = lookup.convert_money(число, из_чего, во_что)
        if деньги:
            return деньги
        return counting.перевести(число, из_чего, во_что)

    def date_now() -> str:
        return f"Сегодня {ru.date()}."

    def abilities() -> str:
        """Ответ на «что ты умеешь» — заранее известный, платить за него незачем."""
        return ABILITIES

    def calculate(выражение: str = "") -> str:
        """Счёт разбором выражения, а не в уме модели и не через eval.

        В уме модель путает проценты и деление, и ошибка звучит ровно так же
        уверенно, как верный ответ. А eval на строке из микрофона — это запуск
        чего угодно, что робот услышал.
        """
        try:
            ответ = counting.посчитать(выражение)
        except ValueError as e:
            return f"Не посчитал: {e}."
        return counting.вслух(ответ)

    def stopwatch(действие: str = "сколько") -> str:
        действие = (действие or "").strip().lower()
        if действие in ("пуск", "старт", "начни", "засеки", "засечь"):
            return секундомер.пуск()
        if действие in ("стоп", "останови", "стой"):
            return секундомер.стоп()
        if действие in ("сброс", "сбрось", "обнули"):
            return секундомер.сброс()
        return секундомер.сколько()

    def random_pick(вид: str = "монетка", снизу: int | None = None,
                    сверху: int | None = None,
                    варианты: list | None = None) -> str:
        """Настоящий бросок. Модель на «загадай число от одного до десяти»
        отвечает семёркой заметно чаще прочего — для игры это подделка."""
        вид = (вид or "").strip().lower()
        if вид.startswith("монет"):
            return counting.монетка()
        if вид.startswith("куб"):
            return counting.кубик(6 if сверху is None else сверху)
        if вид.startswith("выбор") or варианты:
            return counting.выбрать(list(варианты or []))
        # Ноль в границе — настоящий ноль, а не «границу не назвали». Через
        # «or» он превращался в единицу и в десятку: «от нуля до трёх» никогда
        # не давало нуля, а «от минус десяти до нуля» считало до десяти.
        return counting.число(1 if снизу is None else снизу,
                              10 if сверху is None else сверху)

    def volume(change: str = "тише", про_голос: bool = False) -> str:
        """Громкость голоса — но под музыку «сделай тише» почти всегда про неё.

        Развилка та же, что в music_volume, только с другой стороны. Человек
        говорит «сделай тише», не уточняя, и под играющую песню имеет в виду
        песню: свой голос робот и так приглушает, когда играет музыка. Явное
        «говори тише» правило помечает, и тогда это про голос.
        """
        change = (change or "").strip().lower()
        if not про_голос and player is not None and player.играет:
            return music_volume(change)
        if speaker is None:
            return "Громкостью я пока не управляю."
        was = speaker.volume
        if change in ("тише", "потише", "тихо", "меньше"):
            level = max(0.2, round(was - 0.2, 2))
        elif change in ("громче", "погромче", "громко", "больше"):
            level = min(1.0, round(was + 0.2, 2))
        elif change in ("обычная", "нормально", "как обычно", "средне"):
            level = 1.0
        else:
            return "Могу говорить тише или громче."
        speaker.set_volume(level)
        if level == was:
            return "Громче уже некуда." if level >= 1.0 else "Тише уже некуда."
        return "Хорошо, буду тише." if level < was else "Хорошо, погромче."

    def hush() -> str:
        if speaker is None:
            return ""
        speaker.hush()
        log.info("замолчал по просьбе")
        return ""     # молча замолчать — и есть выполнение просьбы

    def repeat() -> str:
        if speaker is None or not speaker.last_said:
            return "Я пока ничего не говорил."
        return speaker.last_said

    def notes_add(item: str) -> str:
        if notes is None:
            return "Список у меня не заведён."
        item = item.strip(" ,.!?")
        if not item:
            return "Что добавить?"
        if not notes.add(item):
            return f"{item.capitalize()} уже в списке."
        return f"Записал: {item}."

    def notes_list() -> str:
        if notes is None:
            return "Список у меня не заведён."
        items = notes.items()
        if not items:
            return "Список пуст."
        return (f"В списке {ru.count(len(items), 'пункт', 'пункта', 'пунктов')}: "
                + ", ".join(items) + ".")

    def notes_remove(item: str) -> str:
        if notes is None:
            return "Список у меня не заведён."
        gone = notes.remove(item)
        if gone is None:
            return f"{item.strip().capitalize()} в списке не нашёл."
        return f"Убрал {gone}."

    def weather(day: str = "сейчас") -> str:
        где = home() if home is not None else place
        if not где or not any(где):
            return ("Я не знаю, в каком мы городе. Скажи «мы в Калининграде» — "
                    "запомню и буду говорить погоду.")
        data = weather_api.fetch(*где)
        if data is None:
            return "Не смог узнать погоду — интернета нет или сервис молчит."
        if day.strip().lower().startswith("завтра"):
            return weather_api.describe_tomorrow(data)
        return weather_api.describe_now(data)

    def notes_clear() -> str:
        if notes is None:
            return "Список у меня не заведён."
        count = notes.clear()
        if not count:
            return "Список и так пуст."
        return f"Стёр весь список, {ru.count(count, 'пункт', 'пункта', 'пунктов')}."

    tools = [
        Tool(
            name="drive",
            description="Проехать в сторону. Колёса меканум: может ехать боком.",
            input_schema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["вперёд", "назад", "влево", "вправо"],
                        "description": "Куда ехать.",
                    },
                    "distance": {
                        "type": "number",
                        "description": "Метров, 0.05–3.0.",
                    },
                },
                "required": ["direction"],
            },
            run=drive,
        ),
        Tool(
            name="turn",
            description="Развернуться на месте.",
            input_schema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["влево", "вправо"],
                        "description": "Влево — против часовой, вправо — по часовой.",
                    },
                    "degrees": {
                        "type": "number",
                        "description": "Градусов, 5–360.",
                    },
                },
                "required": ["direction"],
            },
            run=turn,
        ),
        Tool(
            name="stop",
            description="Немедленно остановить движение.",
            input_schema=EMPTY_SCHEMA,
            run=stop,
        ),
        Tool(
            name="battery",
            description="Узнать текущий заряд батареи.",
            input_schema=EMPTY_SCHEMA,
            run=battery,
        ),
        Tool(
            name="set_timer",
            description="Таймер на N минут. Сработает — робот скажет вслух.",
            input_schema={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "Через сколько минут, 0.1–600.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Короткое название, чтобы отличать таймеры.",
                    },
                },
                "required": ["minutes"],
            },
            run=set_timer,
        ),
        Tool(
            name="time_now",
            description="Текущее время — здесь или в другом городе. Часов у "
                        "тебя нет, а смещение часового пояса по памяти ты "
                        "выдумаешь: не отвечай сам.",
            input_schema={
                "type": "object",
                "properties": {
                    "город": {
                        "type": "string",
                        "description": "Где смотреть время. Пусто — здесь, "
                                       "дома.",
                    },
                },
            },
            run=time_now,
        ),
        Tool(
            name="convert_units",
            description="Перевести меры: длину, вес, объём, время. Зови "
                        "всегда, когда просят перевести одно в другое — в "
                        "уме ты промахнёшься в делении.",
            input_schema={
                "type": "object",
                "properties": {
                    "сколько": {"type": "number", "description": "Сколько переводим."},
                    "из_чего": {"type": "string",
                                "description": "Из какой меры: метр, грамм, "
                                               "фут, литр, час…"},
                    "во_что": {"type": "string", "description": "В какую меру."},
                },
                "required": ["сколько", "из_чего", "во_что"],
            },
            run=convert_units,
        ),
        Tool(
            name="date_now",
            description="Сегодняшняя дата и день недели. Календаря у тебя нет.",
            input_schema=EMPTY_SCHEMA,
            run=date_now,
        ),
        Tool(
            name="list_timers",
            description="Какие таймеры идут и сколько осталось.",
            input_schema=EMPTY_SCHEMA,
            run=list_timers,
        ),
        Tool(
            name="cancel_timer",
            description="Снять таймер. Без названия — если он один.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, если их несколько.",
                    },
                },
            },
            run=cancel_timer,
        ),
        Tool(
            name="set_alarm",
            description="Будильник на время суток. Через N минут — это set_timer.",
            input_schema={
                "type": "object",
                "properties": {
                    "at": {
                        "type": "string",
                        "description": "«7:30», «семь утра», «восемь вечера».",
                    },
                    "label": {
                        "type": "string",
                        "description": "Название, если будильников несколько.",
                    },
                },
                "required": ["at"],
            },
            run=set_alarm,
        ),
        Tool(
            name="set_reminder",
            description="Напоминание: робот произнесёт текст в назначенное время. "
                        "Нужно указать at или minutes.",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "О чём напомнить: «выключить плиту».",
                    },
                    "at": {
                        "type": "string",
                        "description": "Время суток: «8:00», «восемь вечера».",
                    },
                    "minutes": {
                        "type": "number",
                        "description": "Через сколько минут, если время не названо.",
                    },
                },
                "required": ["text"],
            },
            run=set_reminder,
        ),
        Tool(
            name="pause_timer",
            hidden=True,
            description="Приостановить таймер, не сбрасывая его. Без названия — "
                        "если таймер всего один.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, если их несколько.",
                    },
                },
            },
            run=pause_timer,
        ),
        Tool(
            name="resume_timer",
            hidden=True,
            description="Продолжить таймер, снятый с паузы.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Название таймера, если на паузе их несколько.",
                    },
                },
            },
            run=resume_timer,
        ),
        Tool(
            name="cancel_all_timers",
            hidden=True,
            description="Отменить сразу все идущие таймеры. Если их несколько, "
                        "инструмент сначала переспросит — тогда повтори вызов "
                        "с confirmed, но только после согласия человека.",
            input_schema={
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "Человек подтвердил, что снести можно все.",
                    },
                },
            },
            run=cancel_all_timers,
        ),
        Tool(
            name="weather",
            description="Погода там, где стоит робот. По памяти не отвечай. "
                        "Города не знаешь — сначала set_home, потом это.",
            input_schema={
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "enum": ["сейчас", "завтра"],
                        "description": "На когда нужна погода.",
                    },
                },
            },
            run=weather,
        ),
        Tool(
            name="abilities",
            hidden=True,
            description="Рассказать, что робот умеет. Отвечай этим, а не своими "
                        "словами: список умений тут точный.",
            input_schema=EMPTY_SCHEMA,
            run=abilities,
        ),
    ]

    if speaker is not None:
        tools += [
            Tool(
                name="volume",
                hidden=True,
                description="Сделать голос тише или громче.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "change": {
                            "type": "string",
                            "enum": ["тише", "громче", "обычная"],
                            "description": "Куда менять громкость.",
                        },
                    },
                    "required": ["change"],
                },
                run=volume,
            ),
            Tool(
                name="hush",
                hidden=True,
                description="Замолчать немедленно: перестать проговаривать то, "
                            "что уже отправлено, и ничего не отвечать.",
                input_schema=EMPTY_SCHEMA,
                run=hush,
            ),
            Tool(
                name="repeat",
                hidden=True,
                description="Повторить последнюю сказанную фразу.",
                input_schema=EMPTY_SCHEMA,
                run=repeat,
            ),
        ]

    def set_home(город: str) -> str:
        """Запоминает город со слов человека — координаты ищутся сами.

        Раньше широту и долготу полагалось вписать руками в файл настроек, и
        на живом роботе это вышло глупо: человек сказал «я живу в
        Калининграде», а робот ответил «я не знаю, где мы находимся».
        """
        if set_place is None:
            return "Я не умею запоминать город."
        найдено = weather_api.find_city(город)
        if найдено is None:
            return f"Не нашёл такого города — {город}. Скажи иначе."
        имя, lat, lon = найдено
        set_place(имя, lat, lon)
        return f"Запомнил: мы в {имя}."

    def look_up(вопрос: str) -> str:
        from . import lookup
        return lookup.find(вопрос)

    def rates() -> str:
        from . import lookup
        return lookup.rates()

    def play_music(что: str = "", источник: str = "") -> str:
        """Музыка: сначала Яндекс, если он подключён, потом интернет-радио.

        Порядок именно такой. Яндекс умеет то, чего радио не умеет в принципе:
        «поставь Цоя» — это конкретный исполнитель, а не «что-нибудь рок».
        Но он неофициальный и может отвалиться в любой день, поэтому радио
        остаётся не запасным вариантом на будущее, а тем, что играет прямо
        сейчас, если Яндекс промолчал.

        Сказанное вслух слово «радио» этот порядок отменяет: человек попросил
        радио — значит, радио. Иначе «включи радио» уезжало в «Мою волну», а
        «включи радио Рекорд» искало в Яндексе песни со словом «Рекорд».
        """
        if player is None:
            return "Мне некуда играть музыку: пульт не подключён."
        что = " ".join((что or "").split())
        if (источник or "").strip().lower() == "радио":
            return _radio_music(что)
        из_яндекса = _yandex_music(что)
        if из_яндекса:
            return из_яндекса
        return _radio_music(что)

    def _yandex_music(что: str) -> str:
        музыка = getattr(player, "музыка", None)
        if музыка is None or not музыка.possible:
            return ""
        from . import yandex as ya

        if not что:
            # «Включи музыку» без уточнения — это «Моя волна»: она сама
            # подстраивается под лайки, и с каждым разом попадает точнее.
            треки, партия = музыка.wave()
            название = player.очередь(треки, ya.WAVE, партия)
            return f"Ставлю твою волну. {название}." if название else ""

        # Жанр («поставь джаз») Яндекс держит станцией, а не поиском: поиск по
        # слову «джаз» вернул бы песни с этим словом в названии.
        станция = музыка.station(что)
        if станция:
            треки, партия = музыка.wave(станция)
            название = player.очередь(треки, станция, партия)
            if название:
                return f"Включаю {что}. {название}."

        название = player.очередь(музыка.search(что))
        return f"Включаю: {название}." if название else ""

    def _radio_music(что: str) -> str:
        from . import radio as radio_api
        найдено = radio_api.find(что)
        if найдено is None:
            return _не_нашёл(что)
        имя, поток = найдено
        if not player.поток(имя, поток):
            return "Пульт не отвечает, включить музыку некуда."
        return f"Включаю {имя}."

    def _не_нашёл(что: str) -> str:
        """Окончательный отказ — и прямая просьба не подбирать замену.

        На живом роботе просьба «включи музыку Грот» без Яндекса кончилась
        так: радио исполнителя не нашло, ответ «скажи иначе» модель приняла за
        приглашение попробовать ещё, и она подставила сначала «Группа крови»,
        потом «джаз» — и человеку заиграл джаз вместо того, что он просил.
        Четыре вызова инструмента и пятнадцать тысяч токенов на дорогу к
        неверному ответу.

        Поэтому здесь не «скажи иначе», а «это конец», и сказано, почему.
        """
        if not что:
            return ("Каталог радио не отвечает, включить нечего. Скажи это "
                    "человеку, другого способа у тебя нет.")
        музыка = getattr(player, "музыка", None)
        если_бы = ""
        if музыка is None or not музыка.possible:
            если_бы = (" Радиостанции с таким названием нет, а искать по "
                       "исполнителям и песням я умею только через "
                       "Яндекс.Музыку, а она не подключена.")
        return (f"Не нашёл — {что}.{если_бы} Скажи это человеку прямо и НЕ "
                f"подбирай ничего взамен: другой исполнитель или жанр вместо "
                f"того, что просили, — это хуже, чем честное «не нашёл».")

    def stop_music() -> str:
        if player is None:
            return "Музыка и так не играет."
        return player.выключить()

    def music_next() -> str:
        if player is None or not player.играет:
            return "Сейчас ничего не играет."
        return player.дальше() or "Треки кончились."

    def music_volume(изменение: str = "тише", уровень: int = 0,
                     шаг: int = 0) -> str:
        """Громкость музыки — или голоса, если музыки нет.

        Развилка здесь, а не в правиле, намеренно: «убавь звук» человек
        говорит одинаково и про то, и про другое, а что он имел в виду,
        видно только по тому, играет ли сейчас музыка. На живом роботе «Кузя,
        убавь музыку» кончилось тем, что робот её выключил совсем.

        Уровень — по десятибалльной: 1 еле слышно, 10 во всю. Раньше на
        «убавь звук до двух» робот отвечал «звук уменьшен до двух» и не
        трогал ничего: числа передать было нечем, и модель просто повторяла
        услышанное вслух.
        """
        изменение = (изменение or "").strip().lower()
        if player is None or not player.играет:
            return volume(изменение)
        def целое(что) -> int:
            try:
                return int(что or 0)
            except (TypeError, ValueError):
                return 0

        номер, сдвиг = целое(уровень), max(0, целое(шаг))
        if номер:
            return player.на_ступень(номер)
        if изменение in ("тише", "потише", "тихо", "меньше"):
            return player.тише(сдвиг or 1)
        if изменение in ("громче", "погромче", "громко", "больше"):
            return player.громче(сдвиг or 1)
        if изменение in ("обычная", "нормально", "как обычно", "средне"):
            return player.обычная()
        return (f"Могу сделать тише, громче или на любую ступень от 1 до "
                f"{player.СТУПЕНЕЙ}. Сейчас {player.ступень}.")

    def what_is_playing() -> str:
        if player is None:
            return "Сейчас ничего не играет."
        return player.что_играет()

    def news() -> str:
        from . import news as news_api
        return news_api.describe(news_api.headlines(news_url or news_api.URL))

    # Новости и город модели ВИДНЫ, в отличие от таймеров и списка. Причина
    # простая: спрашивают их как попало — «расскажи, какие новости последние
    # были», «а живу я в городе Калининграде», — и правило такое не ловит. На
    # живом роботе это вышло враньём: модель ответила «у меня нет доступа к
    # новостям», хотя доступ есть, просто инструмент был от неё спрятан.
    tools.append(Tool(
        name="news",
        description="Свежие заголовки новостей. Зови, когда спрашивают, что "
                    "нового, что в мире, какие новости. По памяти не отвечай: "
                    "твои новости старые.",
        input_schema=EMPTY_SCHEMA,
        run=news,
    ))
    if set_place is not None:
        tools.append(Tool(
            name="set_home",
            description="Запомнить город, где стоит робот. Зови, когда человек "
                        "сказал, где он живёт или где вы находитесь, — после "
                        "этого заработает погода.",
            input_schema={
                "type": "object",
                "properties": {
                    "город": {
                        "type": "string",
                        "description": "Название города так, как его назвал "
                                       "человек. Падеж не важен.",
                    },
                },
                "required": ["город"],
            },
            run=set_home,
        ))

    tools += [
        Tool(
            name="look_up",
            description="Посмотреть справку в интернете: кто это, что это, "
                        "когда было. Зови вместо ответа по памяти — твоя "
                        "память устарела, а справка свежая.",
            input_schema={
                "type": "object",
                "properties": {
                    "вопрос": {"type": "string",
                               "description": "О чём справка: «Калининград», "
                                              "«Юрий Гагарин», «меканум-колесо»."},
                },
                "required": ["вопрос"],
            },
            run=look_up,
        ),
        Tool(
            name="rates",
            description="Курс доллара и евро по Центробанку. Зови всегда, "
                        "когда спрашивают про курс: по памяти ты назовёшь "
                        "прошлогодний.",
            input_schema=EMPTY_SCHEMA,
            run=rates,
        ),
        Tool(
            name="calculate",
            description="Посчитать. Зови всегда, когда просят что-то "
                        "сосчитать: проценты и деление в уме ты путаешь, а "
                        "ошибка звучит так же уверенно, как верный ответ. "
                        "Переведи услышанное в выражение: «пятнадцать "
                        "процентов от двух тысяч» — это 15/100*2000.",
            input_schema={
                "type": "object",
                "properties": {
                    "выражение": {
                        "type": "string",
                        "description": "Только числа и знаки + - * / % ** и "
                                       "скобки. Слова не понимаю.",
                    },
                },
                "required": ["выражение"],
            },
            run=calculate,
        ),
        Tool(
            name="stopwatch",
            description="Секундомер: пуск, стоп, сброс, «сколько прошло». "
                        "Зови обязательно — сколько времени прошло между "
                        "репликами, ты не знаешь, часов у тебя нет.",
            input_schema={
                "type": "object",
                "properties": {
                    "действие": {
                        "type": "string",
                        "enum": ["пуск", "стоп", "сброс", "сколько"],
                        "description": "Что сделать с секундомером.",
                    },
                },
                "required": ["действие"],
            },
            run=stopwatch,
        ),
        Tool(
            name="random_pick",
            description="Жребий: монетка, кубик, случайное число, выбор из "
                        "нескольких. Зови обязательно — сам ты случайное "
                        "число не выдумаешь, у тебя выйдет одно и то же, а "
                        "человек ждёт настоящего броска.",
            input_schema={
                "type": "object",
                "properties": {
                    "вид": {
                        "type": "string",
                        "enum": ["монетка", "кубик", "число", "выбор"],
                        "description": "Что бросаем.",
                    },
                    "снизу": {"type": "integer",
                              "description": "Для «числа»: от. По умолчанию 1."},
                    "сверху": {"type": "integer",
                               "description": "Для «числа»: до. По умолчанию 10. "
                                              "Для «кубика» — сколько граней."},
                    "варианты": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Для «выбора»: между чем выбрать.",
                    },
                },
                "required": ["вид"],
            },
            run=random_pick,
        ),
    ]
    if player is not None:
        tools += [
            Tool(
                name="play_music",
                description="Включить музыку: конкретного исполнителя или "
                            "песню, жанр, радиостанцию. Без уточнения — "
                            "любимую волну. Зови всегда, когда просят "
                            "что-нибудь включить или поставить из музыки. "
                            "Передавай ровно то, что попросил человек. Если "
                            "не нашлось — так и скажи; не зови этот "
                            "инструмент второй раз с другим исполнителем или "
                            "жанром, поставить не то хуже, чем не поставить.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "что": {"type": "string",
                                "description": "Исполнитель, песня, жанр или "
                                               "станция: «Кино», «Группа крови», "
                                               "«джаз», «Рекорд». Пусто — на твой вкус."},
                        "источник": {
                            "type": "string",
                            "enum": ["любой", "радио"],
                            "description": "«радио» — если человек прямо "
                                           "попросил радио или радиостанцию.",
                        },
                    },
                },
                run=play_music,
            ),
            Tool(
                name="stop_music",
                description="Выключить музыку или радио совсем.",
                input_schema=EMPTY_SCHEMA,
                run=stop_music,
            ),
            Tool(
                name="music_next",
                description="Следующая песня. Зови, когда просят переключить, "
                            "поставить другое, пропустить эту.",
                input_schema=EMPTY_SCHEMA,
                run=music_next,
            ),
            Tool(
                name="music_volume",
                description="Сделать музыку тише или громче. Зови и когда "
                            "просят убавить звук: если музыка играет, речь "
                            "почти всегда про неё. Назвали число — передай "
                            "его в «уровень»; сам громкость не меняй и не "
                            "сообщай о смене, не вызвав этот инструмент.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "изменение": {
                            "type": "string",
                            "enum": ["тише", "громче", "обычная"],
                            "description": "Куда менять громкость.",
                        },
                        "уровень": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "description": "Громкость по десятибалльной: 1 "
                                           "еле слышно, 10 во всю. Ставь, "
                                           "когда назвали число («сделай на "
                                           "семь», «убавь до двух»).",
                        },
                        "шаг": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 9,
                            "description": "На сколько ступеней подвинуть — "
                                           "для «убавь на два». «До двух» "
                                           "это не сюда, а в «уровень».",
                        },
                    },
                    "required": ["изменение"],
                },
                run=music_volume,
            ),
            Tool(
                name="what_is_playing",
                description="Что сейчас играет: исполнитель и название. "
                            "По памяти не отвечай — ты этого не знаешь.",
                input_schema=EMPTY_SCHEMA,
                run=what_is_playing,
            ),
        ]

    if people is not None and who is not None:
        def remember_person(факт: str) -> str:
            # asked=False: сюда модель приходит по своей воле, а не по просьбе
            # «запомни». Прямые просьбы разбираются правилом и до модели не
            # доходят вовсе — там и «Запомнил» вслух уместно, а здесь нет.
            return people.remember(who(), факт, asked=False)

        tools += [
            Tool(
                name="remember_person",
                description=(
                    "Записать в память факт о том, кто сейчас говорит: что "
                    "любит, чем занят, что у него происходит. Зови сам, без "
                    "просьбы, как только услышал о человеке что-то новое, что "
                    "пригодится в следующем разговоре. Вслух об этом не "
                    "объявляй и разрешения не спрашивай — просто запиши и "
                    "продолжай разговор."),
                input_schema={
                    "type": "object",
                    "properties": {
                        "факт": {"type": "string",
                                 "description": "Коротко, одной фразой: "
                                                "«любит крепкий чай», «работает по ночам»."},
                    },
                    "required": ["факт"],
                },
                run=remember_person,
            ),
        ]

    if notes is not None:
        tools += [
            Tool(
                name="notes_add",
                hidden=True,
                description="Добавить пункт в список покупок и дел.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "item": {"type": "string",
                                 "description": "Что записать: «молоко», «оплатить садик»."},
                    },
                    "required": ["item"],
                },
                run=notes_add,
            ),
            Tool(
                name="notes_list",
                hidden=True,
                description="Прочитать вслух список покупок и дел.",
                input_schema=EMPTY_SCHEMA,
                run=notes_list,
            ),
            Tool(
                name="notes_remove",
                hidden=True,
                description="Убрать один пункт из списка.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "item": {"type": "string", "description": "Какой пункт убрать."},
                    },
                    "required": ["item"],
                },
                run=notes_remove,
            ),
            Tool(
                name="notes_clear",
                hidden=True,
                description="Стереть весь список целиком.",
                input_schema=EMPTY_SCHEMA,
                run=notes_clear,
            ),
        ]

    return tools
