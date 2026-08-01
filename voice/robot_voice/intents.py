"""Быстрый разбор простых команд без обращения к модели.

Смысл в том, что «стоп» или «вперёд на метр» — это не задача для языковой
модели. Такие фразы разбираются регулярками мгновенно, бесплатно и предсказуемо,
а модель нужна там, где начинается разговор.

Правила намеренно строгие: совпало — выполняем, не совпало — отдаём модели.
Ошибиться в сторону «не понял и спросил умного» дешевле, чем поехать не туда.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from . import when

log = logging.getLogger(__name__)

# Слова-приставки, которыми люди начинают команду. Их можно просто отбросить.
_PREFIX = r"(?:а\s+)?(?:ну\s+)?(?:давай\s+)?(?:пожалуйста\s+)?"
# Команды, внутри которых едет произвольный текст человека.
_CARRIES_TEXT = re.compile(
    r"(?:а\s+)?(?:ну\s+)?(?:давай\s+)?(?:пожалуйста\s+)?"
    r"(?:напомн|напоминай|добавь|запиши|внеси|убери|вычеркни)")
_VERB_MOVE = r"(?:про)?(?:едь|езжай|поезжай|двигайся|иди|двинься|сдвинься)?"
_VERB_TURN = r"(?:по|раз)?(?:вернись|верни|ворачивайся)?"

_DIR_WORDS = {
    "вперед": "вперёд", "вперёд": "вперёд", "прямо": "вперёд",
    "назад": "назад", "обратно": "назад",
    "влево": "влево", "налево": "влево", "лево": "влево",
    "вправо": "вправо", "направо": "вправо", "право": "вправо",
}

# Расстояния словами — то, что реально говорят вслух. Числительных здесь нет
# намеренно: «два метра» до правил доходит уже как «2 метра», их разбирает
# числовая ветка.
_WORD_DISTANCE = {
    "полметра": 0.5, "пол метра": 0.5, "пол-метра": 0.5,
    "метр": 1.0, "метра": 1.0,
    "чуть": 0.2, "чуть-чуть": 0.2, "немного": 0.3, "немножко": 0.3,
}

# Ключи без предлога: «на» отбрасывается до поиска в словаре.
_WORD_ANGLE = {
    "кругом": 180.0, "пол оборота": 180.0, "полоборота": 180.0,
    "пол-оборота": 180.0, "четверть": 90.0, "полкруга": 180.0,
    "чуть": 30.0, "чуть-чуть": 30.0, "немного": 30.0, "немножко": 30.0,
}


# Числительные словами. Whisper на русском пишет их то цифрами, то словами, а в
# корпусе MASSIVE («через сорок минут», «на десять минут») — почти всегда словами.
_NUM_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "полтора": 1.5, "полторы": 1.5,
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20,
    "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
    "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
}


def numerals_to_digits(text: str) -> str:
    """«сорок пять минут» → «45 минут». Составные числа складываются.

    Так все правила ниже работают с цифрами, и их не приходится дублировать
    под словесную запись.
    """
    out: list[str] = []
    total = 0.0
    started = False

    def flush() -> None:
        nonlocal total, started
        if started:
            out.append(f"{total:g}")
            total, started = 0.0, False

    for word in text.split():
        value = _NUM_WORDS.get(word)
        if value is None:
            flush()
            out.append(word)
        else:
            # «сорок пять» складываем, «пять пять» — нет: второе начинает новое число.
            if started and value >= total:
                flush()
            total += value
            started = True
    flush()
    return " ".join(out)


def normalize(text: str) -> str:
    """Приводит фразу к виду, удобному для регулярок."""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s.,-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,-")
    return numerals_to_digits(t)


def _to_metres(value: str, unit: str) -> float:
    v = float(value.replace(",", "."))
    if unit.startswith("см") or unit.startswith("сантиметр"):
        return v / 100
    return v


@dataclass
class Match:
    """Что распознало правило: имя инструмента и аргументы к нему."""

    tool: str
    args: dict
    rule: str


def parse(text: str) -> Match | None:
    """Разбирает фразу. None — значит это разговор, отдаём модели."""
    t = normalize(text)
    # Длинная фраза почти наверняка не команда — кроме напоминаний и списка,
    # которые несут в себе произвольный текст: «напомни завтра в восемь
    # позвонить в поликлинику и записаться к врачу».
    if not t or len(t) > (140 if _CARRIES_TEXT.match(t) else 60):
        return None

    for rule in _RULES:
        m = rule(t)
        if m is not None:
            log.info("правило %s: %s(%s)", m.rule, m.tool, m.args)
            return m
    return None


# --- сами правила ----------------------------------------------------------
def _stop(t: str) -> Match | None:
    # Слова остановки собраны из русских интентов Home Assistant плюс то,
    # что говорят живому роботу. «Забудь», «неважно», «проехали» сюда
    # намеренно не попали: это отмена просьбы, а не команда встать.
    if re.fullmatch(
        rf"{_PREFIX}(стоп|стоп[- ]стоп|стой|стоять|стой на месте|остановись|"
        rf"(?:при)?останови(?:сь)?|тормози|тормозни|притормози|хватит|прекрати|"
        rf"замри|отставить|отбой)",
        t,
    ):
        return Match("stop", {}, "стоп")
    return None


def _battery(t: str) -> Match | None:
    # Правило было слишком широким: на корпусе MASSIVE оно ловило «включи умную
    # зарядку» и «сколько будет десять процентов от ста». Поэтому теперь —
    # только вопрос о состоянии, без повелительных «включи/выключи», и
    # «зарядка» как устройство отсекается отдельно.
    if re.search(r"\b(зарядк|зарядн|розетк|включи|выключи|подключи)", t):
        return None
    if re.fullmatch(
        rf"{_PREFIX}(?:сколько|какой|каков|какая|что\s+с)?\s*"
        rf"(?:у\s+тебя\s+|там\s+|тво(?:й|его)\s+)?"
        rf"(?:осталось\s+)?"
        rf"(?:заряд\w*|батаре\w*|аккумулятор\w*)"
        rf"(?:\s+(?:осталось|батареи|аккумулятора|в\s+процентах))?",
        t,
    ):
        return Match("battery", {}, "батарея")
    return None


def _drive(t: str) -> Match | None:
    dirs = "|".join(_DIR_WORDS)
    # «проедь вперёд на 1.5 метра», «вперёд», «назад чуть-чуть»
    m = re.fullmatch(
        rf"{_PREFIX}{_VERB_MOVE}\s*({dirs})"
        rf"(?:\s+на)?\s*"
        rf"(?:(\d+(?:[.,]\d+)?)\s*(м|метр\w*|см|сантиметр\w*)|([а-я -]+))?",
        t.strip(),
    )
    if not m:
        return None

    direction = _DIR_WORDS[m.group(1)]
    args: dict = {"direction": direction}

    if m.group(2):
        args["distance"] = _to_metres(m.group(2), m.group(3) or "м")
    elif m.group(4):
        word = m.group(4).strip()
        if word not in _WORD_DISTANCE:
            return None       # непонятный хвост — пусть разбирается модель
        args["distance"] = _WORD_DISTANCE[word]

    return Match("drive", args, "движение")


def _turn(t: str) -> Match | None:
    if re.fullmatch(rf"{_PREFIX}кругом", t):
        return Match("turn", {"direction": "влево", "degrees": 180.0}, "разворот")

    m = re.fullmatch(
        rf"{_PREFIX}(?:по|раз)(?:вернись|верни|ворот)\w*\s*"
        rf"(налево|направо|влево|вправо|лево|право)?"
        rf"(?:\s+на)?\s*"
        rf"(?:(\d+)\s*(?:градус\w*)?|([а-я -]+))?",
        t.strip(),
    )
    if not m:
        return None

    direction = _DIR_WORDS.get(m.group(1) or "", "влево")
    if direction not in ("влево", "вправо"):
        return None

    args: dict = {"direction": direction}
    if m.group(2):
        args["degrees"] = float(m.group(2))
    elif m.group(3):
        # Предлог попадает в захват («на четверть»), а в словаре его нет.
        word = re.sub(r"^на\s+", "", m.group(3).strip())
        if word not in _WORD_ANGLE:
            return None
        args["degrees"] = _WORD_ANGLE[word]

    return Match("turn", args, "разворот")


# Глаголы постановки и снятия таймера — из русских интентов Home Assistant
# и корпуса MASSIVE (там это будильники, но конструкции те же).
_TIMER_SET = (r"(?:установи\w*|постав(?:ь|ить)|включи\w*|запусти\w*|зада(?:й|ть)|"
              r"заведи|засеки|созда(?:й|ть)|сдела(?:й|ть))")
_TIMER_CANCEL = (r"(?:отмени\w*|удали\w*|сбрось\w*|убери|убрать|выключи\w*|"
                 r"останови\w*|отключи\w*|сними|снять)")
_UNIT = r"минут\w*|мин|секунд\w*|сек|час\w*|ч"
# Длительность из одной-трёх частей: «5 минут», «2 часа 30 минут»,
# «2 часа 5 минут и 30 секунд». Союз «и» между частями — обычное дело в речи.
_DURATION = rf"((?:\d+(?:[.,]\d+)?\s*(?:{_UNIT})\b\s*(?:и\s+)?)+)"
# Комната или уточнение места: «на кухне», «в спальне», просто «кухне».
_PLACE = r"(?:(?:в|во|на)\s+)?[а-я]+"

# Вежливое начало вопроса и слова «прямо сейчас» — их можно отбросить.
_ASK = r"(?:(?:скажи|подскажи|назови|напомни)\s+(?:мне\s+)?)?"
_NOW = r"(?:сейчас|сегодня|текущ\w+|сегодняшн\w+)"
_DATE_NOUN = r"(?:дат\w+|числ\w+|день\s+недели|день|дня\s+недели)"


def _unit_to_minutes(value: float, unit: str) -> float:
    if unit.startswith("сек"):
        return value / 60
    if unit.startswith("час") or unit == "ч":
        return value * 60
    return value


def _duration_minutes(text: str) -> float:
    """Складывает все части длительности: «2 часа 5 минут» → 125."""
    total = 0.0
    for value, unit in re.findall(rf"(\d+(?:[.,]\d+)?)\s*({_UNIT})\b", text):
        total += _unit_to_minutes(float(value.replace(",", ".")), unit)
    return round(total, 3)


def _timer(t: str) -> Match | None:
    # «поставь таймер лапша на 9 минут», «задай таймер на 2 часа 5 минут и 30 секунд»
    m = re.fullmatch(
        rf"{_PREFIX}{_TIMER_SET}?\s*(?:мне\s+|для\s+меня\s+)?таймер\s*"
        rf"(?:(?:который\s+)?(?:прозвенит|сработает|зазвонит)\s*)?"
        rf"(?:([а-я][а-я -]{{0,20}}?)\s+)?"          # необязательное название
        rf"(?:(?:на|через)\s*)?"
        rf"{_DURATION}",
        t.strip(),
    )
    if not m:
        return None

    args: dict = {"minutes": _duration_minutes(m.group(2))}
    label = (m.group(1) or "").strip()
    # «на» и «через» — предлоги, а не названия таймера.
    if label and label not in ("на", "через"):
        args["label"] = label
    return Match("set_timer", args, "таймер")


def _list_timers(t: str) -> Match | None:
    # Формулировки статуса — из HassTimerStatus и из alarm_query корпуса
    # MASSIVE («какие будильники я установил», «покажи будильники»).
    if re.fullmatch(
        rf"{_PREFIX}(?:"
        rf"(?:какие\s+)?таймер\w*(?:\s+(?:идут|остались|есть|активны))?|"
        rf"что\s+с\s+таймер\w+(?:\s+(?:на\s+)?(?:{_DURATION}|[а-я][а-я -]{{0,20}}))?|"
        rf"(?:какое\s+)?состояние\s+(?:у\s+)?таймер\w+(?:\s+(?:на\s+)?(?:{_DURATION}|[а-я][а-я -]{{0,20}}))?|"
        rf"сколько\s+(?:времени\s+)?осталось"
        rf"(?:\s+(?:у|на)\s+таймер\w+(?:\s+(?:на\s+)?(?:{_DURATION}|[а-я][а-я -]{{0,20}}))?)?|"
        rf"(?:покажи|скажи|назови|перечисли|прочита\w+)\s+(?:мне\s+)?"
        rf"(?:мои\s+|активные\s+|все\s+)*таймер\w+|"
        rf"список\s+(?:активных\s+|моих\s+)?таймер\w+|"
        rf"какие\s+таймер\w+\s+(?:я\s+)?(?:установ\w+|постав\w+|завед\w+)|"
        rf"есть\s+ли\s+(?:у\s+меня\s+)?(?:какие[- ]нибудь\s+)?таймер\w+"
        rf")",
        t,
    ):
        return Match("list_timers", {}, "список таймеров")
    return None


def _cancel_timers(t: str) -> Match | None:
    # Место в команде («все таймеры на кухне») пропускаем: таймеры у робота
    # общие, комнат он не различает. Отказываться понимать фразу из-за этого
    # глупо — человек всё равно имеет в виду «сними все».
    if re.fullmatch(
        rf"{_PREFIX}{_TIMER_CANCEL}\s+(?:{_PLACE}\s+)?все\s+таймер\w+"
        rf"(?:\s+{_PLACE})?",
        t,
    ):
        return Match("cancel_all_timers", {}, "снять все таймеры")

    # «отмени таймер чай», «выключи таймер на 5 минут», «удали таймер»
    m = re.fullmatch(
        rf"{_PREFIX}{_TIMER_CANCEL}\s+таймер\s*"
        rf"(?:(?:на\s*)?{_DURATION}|([а-я][а-я -]{{0,20}}))?",
        t.strip(),
    )
    if not m:
        return None

    duration, label = m.group(1), (m.group(2) or "").strip()
    if duration:
        # Таймер называют по длительности: «отмени таймер на пять минут».
        # Инструмент сам разберётся — если такого названия нет, а таймер
        # всего один, снимет его.
        return Match("cancel_timer", {"label": duration.strip()}, "снять таймер по времени")
    if not label:
        # «Отмени таймер» без уточнения: если он один — снимаем, если их
        # несколько — инструмент переспросит. Снимать все по такой фразе
        # нельзя, это потеря чужого таймера.
        return Match("cancel_timer", {}, "снять таймер")
    return Match("cancel_timer", {"label": label}, "снять таймер")


def _pause_timers(t: str) -> Match | None:
    # «останови таймер» сюда намеренно не входит: Home Assistant понимает это
    # как паузу, но живому роботу так говорят, когда таймер больше не нужен.
    # Пауза — только там, где о ней сказано прямо.
    m = re.fullmatch(
        rf"{_PREFIX}(?:приостанови\w*|пауза|поставь\s+на\s+паузу)\s+"
        rf"таймер\w*(?:\s+([а-я][а-я -]{{0,20}}))?|"
        rf"{_PREFIX}(?:постав(?:ь|ить)\s+)?таймер\s*([а-я][а-я -]{{0,20}}?)?"
        rf"\s*на\s+паузу|"
        rf"{_PREFIX}пауза\s+таймера",
        t.strip(),
    )
    if m:
        label = (m.group(1) or m.group(2) or "").strip()
        return Match("pause_timer", {"label": label} if label else {}, "пауза таймера")

    m = re.fullmatch(
        rf"{_PREFIX}(?:возобнови\w*|продолжи\w*|запусти\s+заново)\s+"
        rf"таймер\w*(?:\s+([а-я][а-я -]{{0,20}}))?|"
        rf"{_PREFIX}сними\s+таймер\s*([а-я][а-я -]{{0,20}}?)?\s*с\s+паузы",
        t.strip(),
    )
    if m:
        label = (m.group(1) or m.group(2) or "").strip()
        return Match("resume_timer", {"label": label} if label else {}, "продолжить таймер")
    return None


_WAKE_ME = r"(?:разбуд\w+|подним\w+\s+меня|буди)"
_ALARM_NOUN = r"будильник\w*"
_REMIND = r"(?:напомн\w+|напоминай)"
# «через 20 минут» — точно срок. «на 20 минут» — тоже.
_THROUGH = re.compile(rf"через\s+{_DURATION}")
_FOR = re.compile(rf"(?:^|\s)на\s+{_DURATION}")


def _relative_minutes(t: str) -> float | None:
    """Срок в минутах, если он назван как промежуток, а не как время суток."""
    m = _THROUGH.search(t)
    if m:
        return _duration_minutes(m.group(1))
    m = _FOR.search(t)
    # «будильник на 8 часов» — это восемь утра, а не восемь часов ожидания:
    # так говорят про время суток. «на 20 минут» — однозначно срок.
    if m and not re.search(r"\bчас", m.group(1)):
        return _duration_minutes(m.group(1))
    return None


def _alarm(t: str) -> Match | None:
    """Будильник и напоминание: «разбуди в семь», «напомни в восемь позвонить»."""
    is_alarm = re.search(rf"\b{_WAKE_ME}", t) or re.search(rf"\b{_ALARM_NOUN}", t)
    is_remind = re.search(rf"\b{_REMIND}", t)
    if not is_alarm and not is_remind:
        return None

    minutes = _relative_minutes(t)
    at = None if minutes is not None else when.at_time(t)
    if minutes is None and at is None:
        return None      # «напомни мне позвонить маме» — когда? спросит модель

    if is_alarm:
        args = {"at": at.strftime("%H:%M")} if at else {}
        if at is None:
            # «поставь будильник через двадцать минут» — это тот же таймер.
            return Match("set_timer", {"minutes": minutes, "label": "будильник"},
                         "будильник через")
        return Match("set_alarm", args, "будильник")

    text = _reminder_text(t)
    if not text:
        return None      # напомнить-то о чём?
    args = {"text": text}
    if at is not None:
        args["at"] = at.strftime("%H:%M")
    else:
        args["minutes"] = minutes
    return Match("set_reminder", args, "напоминание")


# Всё, что стоит между «напомни» и самим делом: «мне», «завтра», время.
_REMINDER_HEAD = re.compile(
    rf"^{_PREFIX}{_REMIND}\s*(?:мне\s+)?(?:завтра\s+)?"
    rf"(?:через\s+{_DURATION}|(?:в|на|к|во)\s+(?:\d{{1,2}}(?:[ .:-]+\d{{2}})?|час|полдень|полночь)"
    rf"\s*(?:час\w*)?\s*(?:утра|утром|дня|вечера|вечером|ночи|ночью)?)?\s*"
    rf"(?:завтра\s+)?(?:что\s+|чтобы\s+|чтоб\s+)?"
)


def _reminder_text(t: str) -> str:
    return _REMINDER_HEAD.sub("", t, count=1).strip(" ,.!?;:-")


def _abilities(t: str) -> Match | None:
    if re.fullmatch(
        rf"{_PREFIX}(?:"
        rf"что\s+(?:ты\s+)?(?:уме\w+|можешь(?:\s+делать)?)|"
        rf"(?:какие\s+)?тво\w+\s+(?:умения|возможности|функции)|"
        rf"на\s+что\s+ты\s+способен|"
        rf"расскажи\s+(?:мне\s+)?(?:о\s+себе|что\s+уме\w+)|"
        rf"кто\s+ты(?:\s+такой)?"
        rf")",
        t,
    ):
        return Match("abilities", {}, "что умею")
    return None


def _voice_control(t: str) -> Match | None:
    """Громкость, «замолчи», «повтори» — то, что просят каждый день."""
    if re.fullmatch(
        rf"{_PREFIX}(?:замолчи|молчи|тихо|тишина|помолчи|хватит\s+(?:болтать|говорить)|"
        rf"перестань\s+говорить|заткнись)",
        t,
    ):
        return Match("hush", {}, "замолчи")

    m = re.fullmatch(
        rf"{_PREFIX}(?:говори\s+|сделай\s+|стань\s+|будь\s+)?"
        rf"(потише|тише|погромче|громче|обычн\w+\s+громкость\w*|"
        rf"нормальн\w+\s+громкость\w*)"
        rf"(?:\s+(?:говори|пожалуйста))?",
        t,
    )
    if m:
        word = m.group(1)
        if "громкость" in word:
            change = "обычная"
        else:
            change = "тише" if "тише" in word else "громче"
        return Match("volume", {"change": change}, "громкость")

    if re.fullmatch(
        rf"{_PREFIX}(?:повтори(?:\s+(?:ещё\s+раз|пожалуйста))?|"
        rf"что\s+ты\s+сказал\w*|ещё\s+раз|повтори\s+что\s+сказал)",
        t,
    ):
        return Match("repeat", {}, "повтори")
    return None


# «список», «в списке», «из списка» — корень плюс окончание.
_LIST = r"(?:список|списк\w+|перечень|перечн\w+)"

# Чужие области: почта, музыка, календарь, умный дом. Ничего этого у робота
# нет, и «добавь новый контакт» не должно попадать в список покупок. Проверено
# на корпусе MASSIVE — именно такие фразы и лезли.
_NOT_MINE = re.compile(
    r"контакт|письм|почт|мейл|mail|адрес|песн|музык|плейлист|подкаст|радио|"
    r"канал|событи|встреч|календар|уведомлен|напоминани|свет|лампоч|розетк|"
    r"телевизор|пылесос|\bплюс\b|\bминус\b")


def _notes(t: str) -> Match | None:
    """Список покупок и дел — то, за чем к ассистенту идут каждый день."""
    if re.fullmatch(rf"{_PREFIX}(?:очисти|сотри|удали|обнули)\s+(?:весь\s+)?{_LIST}", t):
        return Match("notes_clear", {}, "очистить список")

    if re.fullmatch(
        rf"{_PREFIX}(?:(?:что|чего)\s+(?:там\s+)?в\s+{_LIST}|"
        rf"(?:прочитай|покажи|назови|перечисли|зачитай)\s+(?:мне\s+)?"
        rf"(?:весь\s+|мой\s+)?{_LIST}|"
        rf"{_LIST}\s+покупок|"
        rf"что\s+(?:мне\s+)?(?:нужно|надо)\s+купить)",
        t,
    ):
        return Match("notes_list", {}, "список")

    # «добавь в список молоко», «добавь молоко в список», «запиши молоко»
    m = re.fullmatch(
        rf"{_PREFIX}(?:добавь|запиши|внеси|положи)\s+(?:мне\s+)?"
        rf"(?:в\s+{_LIST}\s+(.+)|(.+?)\s+в\s+{_LIST}|(.+))",
        t,
    )
    if m:
        item = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not item:
            return None
        if m.group(3):
            # «добавь молоко» — без слова «список». Годится, только если это
            # не «добавь песню в плейлист», «добавь встречу в календарь» и не
            # «запиши, что я должен зайти в банк»: у робота нет ни плейлиста,
            # ни календаря, и такие просьбы должна разбирать модель.
            if re.search(r"\bв\b|\bво\b|\bчто\b", m.group(3)):
                return None
            if re.search(r"^(?:запиши|внеси)\b", t) or _NOT_MINE.search(item):
                return None
        return Match("notes_add", {"item": item}, "в список")

    m = re.fullmatch(
        rf"{_PREFIX}(?:убери|вычеркни|удали|выкинь)\s+(?:мне\s+)?"
        rf"(?:из\s+{_LIST}\s+(.+)|(.+?)\s+из\s+{_LIST})",
        t,
    )
    if m:
        item = (m.group(1) or m.group(2) or "").strip()
        if item:
            return Match("notes_remove", {"item": item}, "из списка")
    return None


def _weather(t: str) -> Match | None:
    """Погода. Самый частый вопрос после таймеров — и модель её выдумывает."""
    day_word = r"(?:завтра|на\s+завтра)"
    m = re.fullmatch(
        rf"{_PREFIX}{_ASK}(?:{_NOW}\s+|{day_word}\s+)?(?:"
        rf"как(?:ая|ое)\s+(?:там\s+|{_NOW}\s+|{day_word}\s+)?погод\w*"
        rf"(?:\s+будет)?|"
        rf"погод\w*|"
        rf"что\s+(?:там\s+)?(?:с\s+погодой|на\s+улице)|"
        rf"сколько\s+(?:там\s+|{_NOW}\s+)?градус\w*|"
        rf"(?:тепло|холодно)\s+ли\s+на\s+улице|"
        rf"(?:брать|нужен)\s+(?:ли\s+)?зонт"
        rf")(?:\s+на\s+улице)?(?:\s+(?:{_NOW}|{day_word}))?",
        t,
    )
    if not m:
        return None
    day = "завтра" if "завтра" in t else "сейчас"
    return Match("weather", {"day": day}, "погода")


def _clock(t: str) -> Match | None:
    """Который час и какое сегодня число.

    Часов у модели нет — без инструмента она бы их выдумала. Здесь ответ
    берётся из системных часов робота: мгновенно и бесплатно.

    Про другой город («сколько времени во Владивостоке») правило намеренно
    молчит: часовые пояса — работа для модели.
    """
    if re.fullmatch(
        rf"{_PREFIX}{_ASK}(?:"
        rf"котор\w+\s+(?:{_NOW}\s+)?час|"
        rf"как(?:ое|ая)\s+(?:{_NOW}\s+)?врем\w+|"
        rf"сколько\s+(?:{_NOW}\s+)?врем\w+(?:\s+{_NOW})?|"
        rf"сколько\s+(?:{_NOW}\s+)?на\s+часах|"
        rf"(?:{_NOW}\s+)?врем\w+"
        rf")",
        t,
    ):
        return Match("time_now", {}, "время")

    # «какой сегодня день» — про дату, а не про самочувствие робота.
    if re.fullmatch(
        rf"{_PREFIX}{_ASK}(?:"
        rf"как(?:ое|ая|ой)\s+(?:{_NOW}\s+)?{_DATE_NOUN}(?:\s+{_NOW})?|"
        rf"(?:{_NOW}\s+)?{_DATE_NOUN}(?:\s+{_NOW})?"
        rf")",
        t,
    ):
        return Match("date_now", {}, "дата")
    return None


_RULES: list[Callable[[str], Match | None]] = [
    _stop, _voice_control, _abilities, _weather, _clock, _alarm, _notes,
    _list_timers, _pause_timers, _cancel_timers, _timer,
    _drive, _turn, _battery,
]
