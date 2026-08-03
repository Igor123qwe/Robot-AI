#!/usr/bin/env python3
"""Самопроверка голосового пайплайна — без микрофона, сети и шасси.

Зачем: правила, числа словами и таймеры легко сломать незаметно. Тест гоняет
их на живом коде и печатает по-русски, что именно разошлось.

    cd ~/Robot-AI/voice && python3 selftest.py

Ставить pytest на робота ради этого незачем, поэтому обычный скрипт.
Возвращает 0, если всё сошлось, и 1 если нет, — годится для CI.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Тест должен запускаться чем угодно: и venv-питоном робота, и системным на
# ноутбуке. Микрофон, звуковая карта и клиент модели ему не нужны — если их
# нет, подставляем заглушки. Проверяются правила и логика, а не железо.
_STUBS = {
    "webrtcvad": lambda: types.SimpleNamespace(
        Vad=lambda level: types.SimpleNamespace(is_speech=lambda *a: False)),
    "sounddevice": lambda: types.SimpleNamespace(),
    "anthropic": lambda: types.SimpleNamespace(
        Anthropic=lambda **kw: types.SimpleNamespace(**kw),
        BadRequestError=type("BadRequestError", (Exception,), {}),
        APIConnectionError=type("APIConnectionError", (Exception,), {}),
        APIStatusError=type("APIStatusError", (Exception,), {})),
}
for name, make_stub in _STUBS.items():
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = make_stub()

from robot_voice import ru, weather, when                      # noqa: E402
from robot_voice.brain import HISTORY_LIMIT, Brain, Thinkless, _trim  # noqa: E402
from robot_voice.config import SYSTEM_PROMPT, Config   # noqa: E402
from robot_voice.intents import parse                 # noqa: E402
from robot_voice.notes import Notes                   # noqa: E402
from robot_voice.tools import Timers, build_tools     # noqa: E402
from robot_voice.tts import PhraseCache, WebSpeech    # noqa: E402

FAILED: list[str] = []


def check(what: str, got, expected) -> None:
    if got != expected:
        FAILED.append(f"{what}\n      получено: {got!r}\n      ожидалось: {expected!r}")


def section(title: str) -> None:
    print(f"\n== {title}")


# --------------------------------------------------------------------------
def test_rules() -> None:
    section("правила: фраза → инструмент")
    cases = [
        # остановка
        ("стоп", "stop"), ("стой", "stop"), ("притормози", "stop"),
        ("прекрати", "stop"), ("отставить", "stop"), ("замри", "stop"),
        ("приостанови", "stop"),
        # движение
        ("вперёд", "drive"), ("назад", "drive"), ("влево", "drive"),
        ("вправо", "drive"), ("вперёд на полтора метра", "drive"),
        ("проедь вперёд на 30 см", "drive"), ("назад чуть-чуть", "drive"),
        # разворот
        ("развернись направо", "turn"), ("развернись на 90 градусов", "turn"),
        ("кругом", "turn"), ("развернись на четверть", "turn"),
        # батарея
        ("сколько заряда", "battery"), ("какая батарея", "battery"),
        # таймеры
        ("поставь таймер на 10 минут", "set_timer"),
        ("таймер на 1 час 30 минут", "set_timer"),
        ("задай таймер на 2 часа 5 минут и 30 секунд", "set_timer"),
        ("поставь таймер лапша на 9 минут", "set_timer"),
        ("что с таймерами", "list_timers"),
        ("сколько осталось у таймера", "list_timers"),
        ("отмени таймер лапша", "cancel_timer"),
        ("отмени таймер на пять минут", "cancel_timer"),
        ("отмени таймер", "cancel_timer"),
        ("сбрось все таймеры", "cancel_all_timers"),
        ("выключи все таймеры на кухне", "cancel_all_timers"),
        ("приостанови таймер", "pause_timer"),
        ("поставь таймер на паузу", "pause_timer"),
        ("продолжи таймер", "resume_timer"),
        ("сними таймер с паузы", "resume_timer"),
        # часы
        ("который час", "time_now"), ("сколько времени", "time_now"),
        ("какое сегодня число", "date_now"), ("какой день недели", "date_now"),
        # будильник и напоминания
        ("разбуди меня в семь утра", "set_alarm"),
        ("поставь будильник на 7 30", "set_alarm"),
        ("поставь будильник через двадцать минут", "set_timer"),
        ("напомни через двадцать минут выключить плиту", "set_reminder"),
        ("напомни в восемь вечера позвонить маме", "set_reminder"),
        ("напомни завтра в девять полить цветы", "set_reminder"),
        # голос
        ("что ты умеешь", "abilities"), ("кто ты", "abilities"),
        ("замолчи", "hush"), ("тихо", "hush"),
        ("говори тише", "volume"), ("погромче", "volume"),
        ("обычную громкость", "volume"),
        ("повтори", "repeat"), ("что ты сказал", "repeat"),
        # список
        ("добавь в список молоко", "notes_add"),
        ("добавь молоко в список", "notes_add"),
        ("добавь молоко", "notes_add"),
        ("что в списке", "notes_list"), ("покажи список", "notes_list"),
        ("какая погода", "weather"), ("погода на завтра", "weather"),
        ("сколько градусов", "weather"), ("брать зонт", "weather"),
        ("что нужно купить", "notes_list"),
        ("убери молоко из списка", "notes_remove"),
        ("очисти список", "notes_clear"),
        # Формы, которые раньше не разбирались вовсе и уходили в модель —
        # то есть за деньги и с задержкой в несколько секунд.
        ("таймер на час", "set_timer"),
        ("поставь таймер на полчаса", "set_timer"),
        ("напомни через минуту выключить плиту", "set_reminder"),
        ("отмени таймеры", "cancel_timer"),
        ("повтори ещё раз", "repeat"), ("ещё раз", "repeat"),
        ("добавь в список покупок молоко", "notes_add"),
        ("что в списке покупок", "notes_list"),
        ("напомни в семь разбудить детей", "set_reminder"),
        ("отмени будильник", "cancel_timer"),
    ]
    for phrase, tool in cases:
        m = parse(phrase)
        check(f"«{phrase}»", m.tool if m else None, tool)

    section("правила: что должно уходить модели, а не срабатывать")
    for phrase in ["сколько времени в москве", "какая погода в москве",
                   "сколько будет десять процентов от ста",
                   "включи умную зарядку", "поезжай на кухню",
                   "какой день новогодняя ночь в этом году",
                   "напомни мне позвонить маме",
                   "добавь эту песню в избранное",
                   "добавь новый контакт",
                   "внеси встречу с сашей",
                   "запиши что я должен зайти в банк"]:
        m = parse(phrase)
        check(f"«{phrase}» не правило", m.tool if m else None, None)

    section("правила: аргументы")
    check("вперёд на полтора метра", parse("вперёд на полтора метра").args,
          {"direction": "вперёд", "distance": 1.5})
    check("назад на 30 см", parse("назад на 30 см").args,
          {"direction": "назад", "distance": 0.3})
    check("развернись налево на 90", parse("развернись налево на 90 градусов").args,
          {"direction": "влево", "degrees": 90.0})
    check("таймер 2 часа 5 минут 30 секунд",
          parse("таймер на 2 часа 5 минут и 30 секунд").args, {"minutes": 125.5})
    check("таймер с названием", parse("поставь таймер лапша на 9 минут").args,
          {"minutes": 9.0, "label": "лапша"})
    check("единица без числа — это одна", parse("таймер на час").args,
          {"minutes": 60.0})
    check("полчаса", parse("таймер на полчаса").args, {"minutes": 30.0})
    check("множественное число не становится названием",
          parse("поставь таймеры на 5 минут").args, {"minutes": 5.0})
    check("уточнение списка не попадает в пункт",
          parse("добавь в список покупок молоко").args, {"item": "молоко"})
    check("текст напоминания не съеден будильником",
          parse("напомни в семь разбудить детей").args.get("text"),
          "разбудить детей")


def test_numbers() -> None:
    section("числа словами")
    check("cardinal(0)", ru.cardinal(0), "ноль")
    check("cardinal(21, ж)", ru.cardinal(21, female=True), "двадцать одна")
    check("cardinal(21, ж, вин)", ru.cardinal(21, female=True, accusative=True),
          "двадцать одну")
    check("cardinal(1, ж, вин)", ru.cardinal(1, female=True, accusative=True), "одну")
    check("cardinal(100)", ru.cardinal(100), "сто")
    check("cardinal(360)", ru.cardinal(360), "триста шестьдесят")
    check("plural(1)", ru.plural(1, "минута", "минуты", "минут"), "минута")
    check("plural(2)", ru.plural(2, "минута", "минуты", "минут"), "минуты")
    check("plural(5)", ru.plural(5, "минута", "минуты", "минут"), "минут")
    check("plural(11)", ru.plural(11, "минута", "минуты", "минут"), "минут")
    check("plural(21)", ru.plural(21, "минута", "минуты", "минут"), "минута")
    check("duration(90)", ru.duration(90), "одна минута тридцать секунд")
    check("duration(90, вин)", ru.duration(90, accusative=True),
          "одну минуту тридцать секунд")
    check("duration(540)", ru.duration(540), "девять минут")
    check("duration(7500)", ru.duration(7500), "два часа пять минут")
    check("volts(12.4)", ru.volts(12.4), "двенадцать и четыре вольта")
    check("volts(12.0)", ru.volts(12.0), "двенадцать вольт")
    check("clock(14:35)", ru.clock(datetime(2026, 8, 1, 14, 35)),
          "четырнадцать часов тридцать пять минут")
    check("clock(13:00)", ru.clock(datetime(2026, 8, 1, 13, 0)), "тринадцать часов ровно")
    check("date(01.08.2026)", ru.date(datetime(2026, 8, 1)), "суббота, первое августа")
    check("date(31.12.2026)", ru.date(datetime(2026, 12, 31)),
          "четверг, тридцать первое декабря")


def test_when() -> None:
    section("время будильника")
    now = datetime(2026, 8, 1, 22, 30)
    cases = [
        ("разбуди меня в 7", datetime(2026, 8, 2, 7, 0)),
        ("разбуди меня в 7 утра", datetime(2026, 8, 2, 7, 0)),
        ("будильник на 7 30", datetime(2026, 8, 2, 7, 30)),
        ("в 8 часов вечера", datetime(2026, 8, 2, 20, 0)),
        ("завтра в 8", datetime(2026, 8, 2, 8, 0)),
        ("в 23 15", datetime(2026, 8, 1, 23, 15)),
        ("в час ночи", datetime(2026, 8, 2, 1, 0)),
        ("в час дня", datetime(2026, 8, 2, 13, 0)),
        ("в полночь", datetime(2026, 8, 2, 0, 0)),
        ("в 12 ночи", datetime(2026, 8, 2, 0, 0)),
        ("в 25", None),
        ("без времени вообще", None),
    ]
    for text, expected in cases:
        check(f"«{text}»", when.at_time(text, now), expected)


def test_timers() -> None:
    section("таймеры")
    store = Path(tempfile.mkdtemp()) / "timers.json"
    said: list[str] = []
    timers = Timers(announce=lambda text, **kw: said.append(text), store=store)
    ros = types.SimpleNamespace(voltage=12.4, moving=False, connected=True,
                                drive=lambda *a, **k: None,
                                stop_motion=lambda: None)
    tools = {t.name: t for t in build_tools(ros, timers)}

    check("постановка", tools["set_timer"]({"minutes": 9, "label": "лапша"}),
          "Поставил таймер лапша на девять минут.")
    check("второй безымянный не затирает первый",
          [tools["set_timer"]({"minutes": 5}), tools["set_timer"]({"minutes": 10})],
          ["Поставил таймер на пять минут.",
           "Поставил ещё один таймер, на десять минут."])
    check("сколько идёт", len(timers.remaining()), 3)
    check("пауза", tools["pause_timer"]({"label": "лапша"}),
          "Остановил таймер лапша, на нём девять минут.")
    check("продолжение", tools["resume_timer"]({"label": "лапша"}),
          "Продолжаю таймер лапша, осталось девять минут.")
    check("снять чужой", tools["cancel_timer"]({"label": "чай"}),
          "Таймера чай нет. Есть безымянный, десять минут и лапша. Какой снять?")
    check("снять свой", tools["cancel_timer"]({"label": "лапша"}),
          "Отменил таймер лапша.")

    # переживают перезапуск
    restored = Timers(announce=lambda text, **kw: said.append(text), store=store)
    restored.restore()
    check("пережили перезапуск", sorted(restored.remaining()),
          ["без названия", "десять минут"])
    restored.cancel_all()

    check("батарея", tools["battery"]({}),
          "Батарея двенадцать и четыре вольта, это примерно девяносто три процента.")
    ros.connected = False
    check("без связи с шасси не едем", tools["drive"]({"direction": "вперёд"}),
          "Не могу: нет связи с шасси. Проверь, включён ли робот.")


def test_alarms() -> None:
    section("будильник, напоминание, подтверждение")
    store = Path(tempfile.mkdtemp()) / "timers.json"
    said: list[str] = []
    timers = Timers(announce=lambda text, **kw: said.append(text), store=store)
    ros = types.SimpleNamespace(voltage=12.4, moving=False, connected=True,
                                drive=lambda *a, **k: None, stop_motion=lambda: None)
    speaker = types.SimpleNamespace(volume=1.0, last_said="",
                                    set_volume=lambda v: None, hush=lambda: None)
    tools = {t.name: t for t in build_tools(ros, timers, speaker=speaker)}

    answer = tools["set_alarm"]({"at": "7:30"})
    check("будильник поставлен", answer.startswith("Разбужу"), True)
    check("будильник в списке", "будильник" in timers.remaining(), True)

    answer = tools["set_reminder"]({"text": "выключить плиту", "minutes": 20})
    check("напоминание", answer, "Напомню через двадцать минут.")
    check("напоминание без времени", tools["set_reminder"]({"text": "позвонить"}),
          "Когда напомнить?")

    # снос всех таймеров — только после подтверждения
    check("сначала спросит", tools["cancel_all_timers"]({}),
          "Два таймера. Отменить все?")
    check("таймеры на месте", len(timers.remaining()), 2)
    check("после согласия", tools["cancel_all_timers"]({"confirmed": True}),
          "Отменил все таймеры, их было два.")

    # громкость
    speaker.volume = 1.0
    levels: list[float] = []
    speaker.set_volume = lambda v: (levels.append(v),
                                    setattr(speaker, "volume", v))[0]
    tools["volume"]({"change": "тише"})
    tools["volume"]({"change": "тише"})
    check("тише на два шага", levels, [0.8, 0.6])
    check("обычная", (tools["volume"]({"change": "обычная"}), levels[-1])[1], 1.0)


def test_survives_restart() -> None:
    """Таймеры обязаны пережить остановку сервиса.

    Автообновление перезапускает робота при каждой правке, то есть по
    нескольку раз в день. Раньше обработчик выключения звал cancel_all(),
    а тот вместе с потоками стирал и файл: таймер на духовку пропадал ровно
    в том случае, ради которого хранилище и заведено.
    """
    section("таймеры переживают выключение")
    store = Path(tempfile.mkdtemp()) / "timers.json"
    timers = Timers(announce=lambda text, **kw: None, store=store)
    timers.add("духовка", 600)
    timers.add("чай", 300)

    timers.stop()                       # ровно то, что делает обработчик SIGTERM
    after = Timers(announce=lambda text, **kw: None, store=store)
    after.restore()
    check("пережили выключение", sorted(after.remaining()), ["духовка", "чай"])

    # А вот прямая просьба человека снять всё должна стирать и файл.
    after.cancel_all()
    fresh = Timers(announce=lambda text, **kw: None, store=store)
    fresh.restore()
    check("снятые человеком не воскресают", fresh.remaining(), {})

    # Регистр и ё не должны плодить дубли: «Лапша» и «лапша» — один таймер.
    # Раньше хранились они точной строкой, а искались нестрого, и робот
    # отменял первый попавшийся, бодро отвечая «отменил», — второй шёл дальше.
    двойник = Timers(announce=lambda text, **kw: None, store=store)
    двойник.add("Лапша", 600)
    двойник.add("лапша", 300)
    check("регистр не плодит таймеры", len(двойник.remaining()), 1)
    check("снимается по любому написанию", двойник.cancel("ЛАПША"), True)
    check("после снятия пусто", двойник.remaining(), {})

    # Просроченное объявляем только если это свежая просрочка. Робот стоял
    # неделю — объявлять будильник недельной давности не забота, а испуг.
    from robot_voice.tools import STALE_SECONDS
    сказано: list[str] = []
    старый = Path(tempfile.mkdtemp()) / "timers.json"
    старый.write_text(json.dumps({
        "items": {"духовка": time.time() - STALE_SECONDS - 60,
                  "чайник": time.time() - 60},
        "paused": {}, "messages": {},
    }), encoding="utf-8")
    Timers(announce=lambda text, **kw: сказано.append(text),
           store=старый).restore()
    check("недельную просрочку не объявляем",
          any("духовка" in s for s in сказано), False)
    check("свежую — объявляем", any("чайник" in s for s in сказано), True)


def test_alarm_rules() -> None:
    """Три случая, в которых будильник вёл себя не так, как сказано вслух."""
    section("будильник: отмена, «завтра», затирание")

    # 1. «Отмени будильник» ставил новый будильник на то же время: слово
    #    «будильник» ловилось где угодно, независимо от глагола.
    for phrase in ["отмени будильник", "отмени будильник на семь",
                   "убери будильник", "выключи будильник"]:
        m = parse(phrase)
        check(f"«{phrase}» снимает", m.tool if m else None, "cancel_timer")
    check("«разбуди в семь» по-прежнему ставит",
          parse("разбуди меня в семь утра").tool, "set_alarm")
    check("перенос отдаём модели", parse("перенеси будильник на восемь"), None)

    # 2. «Завтра» терялось по дороге в инструмент, и напоминание срабатывало
    #    на сутки раньше — молча.
    m = parse("напомни завтра в девять полить цветы")
    at = when.moment(m.args["at"])
    check("завтра доехало до инструмента",
          at.date() > datetime.now().date() or at > datetime.now(), True)
    check("время не потерялось", (at.hour, at.minute), (9, 0))

    # «Двенадцать утра» по-английски полночь, по-русски полдень. Робот
    # разбудил бы посреди ночи вместо обеда.
    полдень = when.moment("двенадцать утра", datetime(2026, 8, 2, 8, 0))
    check("двенадцать утра — полдень", полдень.hour, 12)
    полночь = when.moment("двенадцать ночи", datetime(2026, 8, 2, 8, 0))
    check("двенадцать ночи — полночь", полночь.hour, 0)

    # 3. «Будильник через двадцать минут» молча стирал уже стоящий подъём.
    store = Path(tempfile.mkdtemp()) / "timers.json"
    timers = Timers(announce=lambda text, **kw: None, store=store)
    ros = types.SimpleNamespace(voltage=12.4, moving=False, connected=True,
                                drive=lambda *a, **k: None, stop_motion=lambda: None)
    tools = {t.name: t for t in build_tools(ros, timers)}
    tools["set_alarm"]({"at": "7:00"})
    tools["set_timer"](parse("поставь будильник через двадцать минут").args)
    check("подъём на семь утра уцелел", len(timers.remaining()), 2)


def test_weather() -> None:
    section("погода: разбор ответа open-meteo")
    # Сеть в тесте не поднять, но формулировки проверить надо: именно они
    # звучат вслух, и именно в них легко наврать падежом.
    data = {
        "current": {"temperature_2m": 21.4, "weather_code": 3, "wind_speed_10m": 9.2},
        "daily": {"temperature_2m_max": [24, 17.6],
                  "temperature_2m_min": [15, 11.2],
                  "weather_code": [1, 63]},
    }
    check("сейчас", weather.describe_now(data),
          "Сейчас на улице двадцать один градус, пасмурно, "
          "ветер девять метров в секунду.")
    check("завтра", weather.describe_tomorrow(data),
          "Завтра днём восемнадцать градусов, ночью одиннадцать градусов, дождь. "
          "Возьми зонт.")
    check("мороз", weather.describe_now(
        {"current": {"temperature_2m": -3.2, "weather_code": 71}}),
        "Сейчас на улице минус три градуса, небольшой снег.")
    check("пустой ответ", weather.describe_now({}), "Погода не отвечает, попробуй позже.")


def test_notes() -> None:
    section("список")
    store = Path(tempfile.mkdtemp()) / "notes.json"
    notes = Notes(store)
    check("добавили", notes.add("молоко"), True)
    check("дубль не добавился", notes.add("Молоко"), False)
    notes.add("хлеб")
    check("убрали", notes.remove("МОЛОКО"), "молоко")
    check("чужого нет", notes.remove("сыр"), None)
    check("пережил перезапуск", Notes(store).items(), ["хлеб"])


def test_unsure_does_not_drive() -> None:
    """Неуверенно расслышанная фраза не должна двигать робота.

    На живом роботе whisper услышал «Кузяка идла», модель домыслила из этого
    «влево», и робот поехал. Имя при этом совпало, так что проверка «звали ли
    по имени» не спасла — нужна вторая, про уверенность распознавания.

    Так устроено у всех, у кого команда может что-то сдвинуть: Home Assistant
    не пускает в модель то, что разобрал сам, а Алиса перед разговорной веткой
    прогоняет реплику через отдельный классификатор.
    """
    section("неуверенное распознавание не двигает робота")
    from robot_voice.app import Addressed

    store = Path(tempfile.mkdtemp())
    ros = types.SimpleNamespace(voltage=12.4, moving=False, busy=False,
                                connected=True, drive=lambda *a, **k: None,
                                stop_motion=lambda: None)
    addressed = Addressed()
    tools = {t.name: t for t in build_tools(
        ros, Timers(lambda *a, **k: None, store=store / "t.json"),
        addressed=addressed)}

    addressed.by_name, addressed.sure = True, True
    check("уверенно и по имени — едем",
          tools["drive"]({"direction": "вперёд"}).startswith("Еду"), True)

    addressed.sure = False
    check("расслышали плохо — не едем",
          tools["drive"]({"direction": "вперёд"}),
          "Не уверен, что расслышал. Повтори, пожалуйста.")
    check("и разворот тоже",
          tools["turn"]({"direction": "влево"}),
          "Не уверен, что расслышал. Повтори, пожалуйста.")

    addressed.by_name, addressed.sure = False, True
    check("без имени — прежний ответ",
          tools["drive"]({"direction": "вперёд"}),
          "Для поездки позови меня по имени.")

    # «Стоп» не должен ни от чего зависеть: он останавливает, а не запускает.
    addressed.by_name = addressed.sure = False
    check("стоп работает всегда", tools["stop"]({}).endswith("."), True)


def test_slicing() -> None:
    """Нарезка на фразы: что уезжает в распознавание, а что нет.

    Два порога тут не работали вовсе. Одиночный щелчок проходил как секунда
    почти-тишины, потому что «речевыми» считались и предбуфер, и семьсот
    миллисекунд тишины на конце. А непрерывный шум длиннее двадцати секунд
    не отбрасывался, а отдавался как обычная фраза — и робот замолкал на
    минуту, потому что распознавание идёт втрое дольше самого звука.
    """
    section("нарезка на фразы")
    import threading as th
    from collections import deque

    import numpy as np

    from robot_voice import audio

    def прогон(схема) -> list[float]:
        """Схема — список (речь?, сколько кадров). Возвращает длины фраз."""
        кадры, признаки = [], []
        for речь, n in схема:
            for _ in range(n):
                кадры.append(np.zeros(160, dtype=np.int16))
                признаки.append(речь)
        подряд = iter(признаки)
        l = audio.Listener.__new__(audio.Listener)
        l.pump = types.SimpleNamespace(start=lambda: None,
                                       frames=lambda: iter(кадры))
        l.sample_rate = 16000
        l.vad = types.SimpleNamespace(is_speech=lambda *a: next(подряд))
        l.silence_frames, l.min_speech_frames = 35, 15     # 700 мс, 300 мс
        l.max_speech_frames = 1000                          # 20 с
        l.start_frames, l.preroll = 2, deque(maxlen=15)
        l._muted = th.Event()
        return [round((len(w) - 44) / 2 / 16000, 1) for w in l.utterances()]

    # Признак «микрофон на связи». Сервер робота подсыпает ровные нули, чтобы
    # соединение не уснуло, и по «кадры идут» источник выглядел живым всегда:
    # робот бодро сообщал «микрофон на связи» и при закрытой вкладке.
    from robot_voice.audio import BrowserSource, Pump
    источник = BrowserSource("http://тест", 16000)
    насос = Pump(источник)
    насос.online = True
    check("одна тишина — микрофон не считается живым", насос.alive, False)
    источник._real_at = __import__("time").monotonic()
    check("пришёл настоящий звук — живой", насос.alive, True)
    check("источник без своего мнения не мешает",
          Pump(types.SimpleNamespace()).alive, False)

    check("одиночный щелчок отброшен",
          прогон([(False, 20), (True, 2), (False, 40)]), [])
    check("настоящая фраза проходит",
          len(прогон([(False, 20), (True, 50), (False, 40)])), 1)
    check("двадцать секунд шума не уезжают в распознавание",
          прогон([(True, 1000), (False, 40)]), [])
    check("после отброшенного куска слух возвращается",
          прогон([(True, 1000), (False, 40), (True, 50), (False, 40)])[-1] < 2.0,
          True)


def test_stop_while_thinking() -> None:
    """«Стоп», сказанный пока робот думает, не должен пропадать.

    Главный цикл в это время висит в разговоре с моделью и фраз не читает, а
    модель могла уже отправить робота ехать — поездка длится до пятнадцати
    секунд. Мгновенной остановки тут нет и не обещается, но потерять команду
    совсем нельзя.
    """
    section("«стоп» из паузы не теряется")
    from robot_voice.app import _caught_stop

    stopped: list[str] = []
    ros = types.SimpleNamespace(moving=True,
                                stop_motion=lambda: stopped.append("стоп"))
    # Распознаватель тут простой: что подсунули, то и «услышал».
    ears = types.SimpleNamespace(transcribe=lambda wav: wav.decode())

    check("«стоп» пойман", _caught_stop("стоп".encode(), ears, ros), True)
    check("колёса встали", stopped, ["стоп"])

    stopped.clear()
    check("обычная фраза не останавливает",
          _caught_stop("а сколько времени".encode(), ears, ros), False)
    check("мусор не останавливает", _caught_stop("...".encode(), ears, ros), False)
    check("пустой хвост не разбираем", _caught_stop(b"", ears, ros), False)

    ros.moving = False
    check("стоим — разбирать нечего", _caught_stop("стоп".encode(), ears, ros), False)
    check("лишних остановок не было", stopped, [])

    # Сбой распознавания не должен ронять ответ: это происходит в блоке,
    # который выполняется уже после того, как робот всё сказал.
    падучий = types.SimpleNamespace(
        transcribe=lambda wav: (_ for _ in ()).throw(RuntimeError("нет связи")))
    ros.moving = True
    check("сбой распознавания не роняет", _caught_stop("звук".encode(), падучий, ros), False)


def test_speech_streams() -> None:
    """Первое предложение уходит в пульт, пока модель ещё договаривает.

    Раньше реплика копилась целиком и синтез начинался только после
    последнего слова: вся потоковая машинерия не давала ничего, а две-три
    секунды синтеза честно ждали своей очереди за десятью секундами
    генерации. Проверяется тем, что клип приходит ДО close().
    """
    section("речь идёт параллельно с генерацией")
    import http.server
    import threading as th

    got: list[bytes] = []
    first = th.Event()

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append(self.rfile.read(n))
            first.set()
            body = b'{"listeners": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Catch)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{srv.server_address[1]}/speak"

    folder = Path(tempfile.mkdtemp())
    cache = PhraseCache(folder, "тест")
    # Кладём звук заранее — тогда piper не понадобится, и проверка поедет
    # где угодно, хоть на сборщике без синтеза.
    тишина = b"\x00\x00" * 2205          # 0.1 с при 22050 Гц
    cache.put("Раз.", тишина)
    cache.put("Два.", тишина)

    speech = WebSpeech(["не-запускается"], 22050, endpoint, 1.0, cache=cache)
    try:
        speech.feed("Раз.")
        check("первое предложение ушло, не дожидаясь конца",
              first.wait(timeout=10), True)
        speech.feed("Два.")
        check("обе реплики доехали", (speech.close(), len(got)), (1, 2))
        check("это WAV", got[0][:4], b"RIFF")

        # Кэш переживает перезапуск — иначе он бесполезен: робот
        # перезапускается при каждой правке.
        check("кэш на диске", PhraseCache(folder, "тест").get("Раз."), тишина)
        check("чужой голос не подходит",
              PhraseCache(folder, "другой").get("Раз."), None)
    finally:
        srv.shutdown()


def test_pc_url() -> None:
    """Одна настройка на всё: адрес ПК включает и разговор, и распознавание.

    Разнести их по разным строкам легко, а забыть одну — ещё легче, и тогда
    робот молча продолжит распознавать сам, вчетверо медленнее, а человек
    будет думать, что ПК подключён.
    """
    section("адрес ПК включает и разговор, и распознавание")
    import os

    было = {k: os.environ.get(k) for k in
            ("ROBOT_PC_URL", "ROBOT_LOCAL_API_BASE", "ROBOT_STT_URL")}
    try:
        os.environ["ROBOT_PC_URL"] = "http://192.168.0.5:4000/"
        os.environ.pop("ROBOT_LOCAL_API_BASE", None)
        os.environ.pop("ROBOT_STT_URL", None)
        cfg = Config()
        check("разговор", cfg.local_api_base, "http://192.168.0.5:4000")
        check("распознавание", cfg.stt_url, "http://192.168.0.5:4000/stt")
        check("без ключа старт разрешён", (cfg.api_key or "") == "" and
              cfg.check() is None, True)

        # Заданное явно главнее: значит человек знает, зачем разносил.
        os.environ["ROBOT_STT_URL"] = "http://другой:9000/stt"
        check("явный адрес не перебивается", Config().stt_url, "http://другой:9000/stt")
    finally:
        for k, v in было.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_hidden() -> None:
    """Скрытый от модели инструмент обязан вызываться правилом.

    Схемы инструментов уезжают в каждый запрос и оплачиваются каждый раз,
    поэтому те, что надёжно разбираются правилами, модели не показываются.
    Цена ошибки высокая: скрыть инструмент, до которого правило не достаёт, —
    значит потерять умение совсем, причём молча.
    """
    section("скрытые от модели инструменты")
    store = Path(tempfile.mkdtemp())
    timers = Timers(announce=lambda text, **kw: None, store=store / "timers.json")
    ros = types.SimpleNamespace(voltage=12.4, moving=False, connected=True,
                                drive=lambda *a, **k: None, stop_motion=lambda: None)
    speaker = types.SimpleNamespace(volume=1.0, last_said="",
                                    set_volume=lambda v: None, hush=lambda: None)
    tools = build_tools(ros, timers, speaker=speaker,
                        notes=Notes(store / "notes.json"))
    hidden = {t.name for t in tools if t.hidden}
    phrases = [
        "приостанови таймер", "продолжи таймер", "сбрось все таймеры",
        "что ты умеешь", "говори тише", "замолчи", "повтори",
        "добавь в список молоко", "что в списке", "убери молоко из списка",
        "очисти список",
    ]
    reachable = {m.tool for m in (parse(p) for p in phrases) if m is not None}
    check("до каждого скрытого достают правила", sorted(hidden - reachable), [])
    visible = [t.spec() for t in tools if not t.hidden]
    print(f"   модель видит {len(visible)} инструментов из {len(tools)}, "
          f"{len(json.dumps(visible, ensure_ascii=False))} символов схем "
          f"плюс {len(SYSTEM_PROMPT)} символов промпта")


def test_thinkless() -> None:
    """Размышления модели не должны звучать вслух.

    Локальные модели выводят ход мысли прямо в текст, между <think> и
    </think>. Робот прочитает это вслух — полминуты рассуждений вместо
    ответа. Тег при этом легко разрывается между кусками потока, и именно
    на разрыве такой фильтр обычно и ломается.
    """
    section("размышления модели не звучат вслух")

    def run(chunks: list[str]) -> str:
        said: list[str] = []
        f = Thinkless(said.append)
        for c in chunks:
            f.feed(c)
        f.close()
        return "".join(said)

    check("без размышлений — как есть", run(["Привет", ", Игорь"]), "Привет, Игорь")
    check("размышления вырезаны",
          run(["<think>надо посчитать</think>Восемь часов"]), "Восемь часов")
    # Главный случай: Qwen3 не пишет открывающий тег — он уже в шаблоне
    # запроса, и ответ начинается сразу внутри размышлений.
    check("закрывающий тег без открывающего",
          run(["Надо ответить коротко. ", "Пожалуй, так.", "</think>", "Привет!"]),
          "Привет!")
    check("закрывающий разорван между кусками",
          run(["думаю", "</thi", "nk>", "Готово."]), "Готово.")
    # Длинный ответ без размышлений не должен застрять в придержанном начале.
    длинный = "а" * 500
    check("длинный ответ доходит целиком", run([длинный]), длинный)
    check("тег разорван между кусками",
          run(["<thi", "nk>", "думаю", "</thi", "nk>", "Готово"]), "Готово")
    check("текст до и после",
          run(["Сейчас", "<think>ага</think>", " посмотрю"]), "Сейчас посмотрю")
    check("угловая скобка не от тега",
          run(["Цена < 5 рублей"]), "Цена < 5 рублей")
    check("размышления не закрылись — вслух ничего",
          run(["<think>завис на полуслове"]), "")
    # Хвост, похожий на начало тега, придерживается — но если ответ на нём
    # кончился, его надо договорить, а не проглотить.
    check("недописанный хвост договаривается", run(["Готово<"]), "Готово<")


def test_endpoints() -> None:
    """ПК основной, облако запасное: выключенный ПК не должен ломать разговор.

    Это главное свойство схемы «мозг на компьютере». Проверять его руками
    неудобно — надо выключать ПК, — а сломать легко: достаточно, чтобы
    исключение поехало не в ту ветку, и робот замолчит навсегда.
    """
    section("ПК основной, облако запасное")

    def answer(text: str):
        """Ответ модели: поток кусков плюс итоговое сообщение."""
        class Stream:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self):
                yield types.SimpleNamespace(
                    type="content_block_delta",
                    delta=types.SimpleNamespace(type="text_delta", text=text))
            def get_final_message(self):
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text=text)],
                    stop_reason="end_turn",
                    usage=types.SimpleNamespace(input_tokens=100, output_tokens=10))
        return Stream()

    def client(reply):
        """Строка — ответ. None — молчит. Исключение — отвечает, но плохо."""
        def stream(**kw):
            if reply is None:
                raise _no_connection()
            if isinstance(reply, Exception):
                raise reply
            return answer(reply)
        return types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))

    def brain(pc, cloud) -> Brain:
        cfg = Config()
        cfg.local_api_base = "http://пк:4000"
        cfg.api_key = cfg.local_api_key = "x"
        b = Brain(cfg, [])
        b.endpoints[0].client = client(pc)
        b.endpoints[1].client = client(cloud)
        return b

    b = brain(pc="с ПК", cloud="из облака")
    check("ПК включён — отвечает он", b.reply("привет", lambda s: None), "с ПК")
    check("бесплатный ответ в деньги не пишем", (b.total_in, b.total_out), (0, 0))

    b = brain(pc=None, cloud="из облака")
    check("ПК выключен — отвечает облако",
          b.reply("привет", lambda s: None), "из облака")
    check("платный ответ посчитан", b.total_in > 0, True)
    check("молчащий ПК отложен", b.endpoints[0].down_until > 0, True)
    # Второй раз к ПК не ходим вовсе — иначе каждая фраза ждала бы связи.
    check("выключенный ПК больше не дёргаем",
          [e.name for e in b._live()], [b.endpoints[1].name])

    # ПК ответил, но плохо: модель не скачана в Ollama (404), сама Ollama не
    # запущена (500), опечатка в имени модели (400). Раньше ловился только
    # обрыв связи, и такая ошибка убивала ход целиком — облако не пробовалось
    # ни разу, и робот отвечал «что-то пошло не так» на каждую фразу.
    for кто, сбой in (("модель не скачана", _http_error(404)),
                      ("Ollama не запущена", _http_error(500)),
                      ("опечатка в имени модели", _http_error(400))):
        b = brain(pc=сбой, cloud="из облака")
        check(f"{кто} — отвечает облако",
              b.reply("привет", lambda s: None), "из облака")
        check(f"{кто} — ПК отложен", b.endpoints[0].down_until > 0, True)

    # Без облачного ключа облачного собеседника быть не должно: вместо обрыва
    # связи оттуда прилетит отказ авторизации, и запасного пути не станет.
    cfg = Config()
    cfg.local_api_base = "http://пк:4000"
    cfg.api_key = ""
    cfg.local_api_key = "x"
    check("без ключа облака нет", len(Brain(cfg, []).endpoints), 1)

    b = brain(pc=None, cloud=None)
    try:
        b.reply("привет", lambda s: None)
        check("оба молчат — ошибка наверх", "не упало", "APIConnectionError")
    except Exception as e:
        check("оба молчат — ошибка наверх", type(e).__name__, "APIConnectionError")


def test_brain_money() -> None:
    """Три места, где мозг терял деньги или делал работу дважды."""
    section("мозг: история, кэш, молчание")
    import anthropic
    from robot_voice.brain import Endpoint

    # 1. Сбой посреди хода с инструментами. Инструмент уже сработал
    #    по-настоящему, и если ход не попал в историю, человек повторит
    #    просьбу — и будильник поставится второй раз.
    cfg = Config()
    cfg.api_key = "x"
    cfg.local_api_base = ""
    сделано: list[str] = []
    b = Brain(cfg, [])
    b.tools = {"set_alarm": lambda args: (сделано.append("будильник"),
                                          "Разбужу в семь.")[1]}

    круги = iter([
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="tool_use", id="t1",
                                           name="set_alarm", input={})],
            stop_reason="tool_use", usage=None),
    ])

    def сбой(messages, on_text):
        try:
            return b.endpoints[0], next(круги)
        except StopIteration:
            raise _no_connection()

    b._round = сбой
    try:
        b.reply("разбуди в семь", lambda s: None)
    except Exception:
        pass
    check("инструмент сработал один раз", сделано, ["будильник"])
    check("ход попал в историю, несмотря на сбой",
          any(m.get("role") == "assistant" for m in b.history), True)

    # 2. Кэш промпта выключался на ЛЮБУЮ четырёхсотую и навсегда: полторы
    #    тысячи токенов начинали оплачиваться целиком в каждой реплике.
    ep = Endpoint(name="тест", client=None, model="м", effort="low")
    check("посторонняя ошибка кэш не трогает",
          (Brain._degrade(ep, Exception("prompt is too long")), ep.use_cache),
          (False, True))
    check("отказ именно от кэша — отступаем",
          (Brain._degrade(ep, Exception("cache_control not supported")),
           ep.use_cache), (True, False))
    check("отказ от effort — убираем",
          (Brain._degrade(ep, Exception("unknown field output_config")), ep.effort),
          (True, ""))

    # 3. Отказ модели робот проглатывал молча: строка возвращалась, но никто
    #    её не озвучивал.
    сказано: list[str] = []
    b2 = Brain(cfg, [])
    b2._round = lambda messages, on_text: (
        b2.endpoints[0],
        types.SimpleNamespace(content=[], stop_reason="refusal", usage=None))
    b2.reply("что-то нехорошее", сказано.append)
    check("на отказ робот говорит вслух", "".join(сказано).startswith("Извини"), True)


def _http_error(code: int) -> Exception:
    """Ответ с HTTP-кодом так, как его создаёт SDK."""
    import anthropic
    kinds = {400: "BadRequestError", 404: "NotFoundError",
             500: "InternalServerError"}
    cls = getattr(anthropic, kinds[code], None)
    if cls is None:                       # заглушка вместо настоящего SDK
        return anthropic.APIConnectionError()
    try:
        import httpx
        return cls(
            f"код {code}",
            response=httpx.Response(
                code, request=httpx.Request("POST", "http://пк:4000/v1/messages")),
            body=None)
    except Exception:
        return cls.__new__(cls)


def _no_connection() -> Exception:
    """Обрыв связи так, как его создаёт SDK, а где нельзя — как получится."""
    import anthropic
    try:
        import httpx
        return anthropic.APIConnectionError(
            request=httpx.Request("POST", "http://пк:4000/v1/messages"))
    except Exception:
        return anthropic.APIConnectionError()


def test_history() -> None:
    section("обрезка истории диалога")

    def turn(rounds: int) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "текст"}]
        for _ in range(rounds):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use"}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text"}]})
        return msgs

    for rounds, limit in ((0, HISTORY_LIMIT), (3, HISTORY_LIMIT), (6, 14)):
        history: list[dict] = []
        for _ in range(8):
            history = _trim(history + turn(rounds))
        check(f"{rounds} вызовов инструментов за ход: история не растёт",
              len(history) <= limit, True)
        check(f"{rounds} вызовов: история начинается с реплики человека",
              history[0]["role"] == "user" and isinstance(history[0]["content"], str),
              True)


def test_names() -> None:
    """Ищет обращения к тому, что забыли импортировать.

    Такое не ловится ни компиляцией, ни остальными проверками: код падает
    только когда до строки доходит выполнение. Один раз так и вышло —
    `datetime.now()` в стартовом логе при отсутствующем импорте, сервис
    поднимался и падал по кругу триста раз.

    Работает на stdlib: symtable знает, какое имя в функции считается
    глобальным, а какое — локальным.
    """
    import builtins
    import symtable

    section("забытые импорты")
    here = Path(__file__).resolve().parent
    files = sorted((here / "robot_voice").glob("*.py")) + [here / "selftest.py"]
    # Веб-слой и ПК тоже. В режиме browser через веб-сервер идут И микрофон,
    # И динамик: забытый импорт там делает робота разом глухим и немым, а
    # голосовой сервис при этом бодро работает и не жалуется — обрывы звука
    # он считает нормой. На ПК то же самое: мозг молча перестанет отвечать.
    files += sorted((here.parent / "web").glob("*.py"))
    files += sorted((here.parent / "pc").glob("*.py"))

    # Имена, которые питон заводит в каждом модуле сам. В таблице символов их
    # нет, пока в них не пишут, — а обращаться к ним законно.
    dunder = {"__file__", "__name__", "__doc__", "__package__",
              "__spec__", "__loader__", "__builtins__"}

    for path in files:
        top = symtable.symtable(path.read_text(), str(path), "exec")
        known = {s.get_name() for s in top.get_symbols()} | set(dir(builtins)) | dunder
        missing: list[str] = []

        def walk(table, missing=missing, known=known):
            for sym in table.get_symbols():
                if sym.is_global() and sym.get_name() not in known:
                    missing.append(f"{table.get_name()}: {sym.get_name()}")
            for child in table.get_children():
                walk(child)

        for child in top.get_children():
            walk(child)
        check(f"{path.name}: всё импортировано", sorted(set(missing)), [])


def test_dialogue() -> None:
    """Прогон главного цикла целиком: фразы на входе, реплики на выходе.

    Без микрофона, модели и шасси — всё вокруг цикла подменено. Именно здесь
    ловятся ошибки порядка: «отбой» на ходу усыплял робота вместо остановки,
    а «стой» во время переспроса считалось ответом на вопрос.
    """
    section("разговор целиком")
    from robot_voice import app                                  # noqa: E402
    from robot_voice.config import Config                        # noqa: E402

    said: list[str] = []
    moved: list[str] = []

    class FakeListener:
        pump = types.SimpleNamespace(online=True)

        def __init__(self, phrases): self.phrases = phrases
        def utterances(self): return iter([p.encode() for p in self.phrases])
        def mute(self): pass
        def unmute(self): pass

    class FakeVoice:
        speaker = types.SimpleNamespace(stream=lambda **kw: None, last_said="")
        def say(self, text, **kw): said.append(text); return 1
        def heard(self, text): pass
        def hold(self): return contextlib_nullcontext()
        def quiet(self): return contextlib_nullcontext()

    import contextlib as _c
    contextlib_nullcontext = _c.nullcontext

    class FakeBrain:
        last_talk = 0.0
        def __init__(self): self.asked: list[str] = []
        def reply(self, text, on_text):
            self.asked.append(text)
            on_text("Отвечаю модели.")
            return "Отвечаю модели."
        def reset(self): pass

    class FakeRos:
        """Едет ровно до команды «стоп» — как настоящее шасси."""

        voltage, connected = 12.4, True

        @property
        def moving(self): return bool(moved) and moved[-1] == "drive"
        def drive(self, *a, **k): moved.append("drive")
        def stop_motion(self): moved.append("stop")

    ros = FakeRos()
    timers = Timers(announce=lambda t, **k: 1,
                    store=Path(tempfile.mkdtemp()) / "t.json")
    addressed = app.Addressed()
    tools = build_tools(ros, timers, addressed=addressed)

    phrases = [
        "Кузя вперёд",             # по имени — едет
        "прямо",                   # без имени — не поедет
        "Кузя отбой",              # это команда остановки, а не прощание
        "Кузя поставь таймер на пять минут",
        "отмена",                  # снимает только что поставленный таймер
        "Кузя что ты умеешь",
        "Кузя расскажи про космос",   # это к модели
        "спасибо",                 # прощание
        "вперёд",                  # уже спит — не реагирует
    ]
    brain = FakeBrain()
    cfg = Config()
    app._listen_loop(cfg, FakeListener(phrases), types.SimpleNamespace(
        transcribe=lambda wav: wav.decode()), brain, FakeVoice(), tools, addressed)

    check("поехал по имени", said[0].startswith("Еду вперёд"), True)
    check("без имени не поехал", said[1], "Для поездки позови меня по имени.")
    check("«отбой» остановил, а не усыпил", said[2], "Остановился.")
    check("колёса действительно встали", moved[-1], "stop")
    check("таймер поставлен", said[3], "Поставил таймер на пять минут.")
    check("«отмена» сняла таймер", said[4], "Отменил таймер.")
    check("таймеров не осталось", timers.remaining(), {})
    check("умения — из правила", said[5].startswith("Я умею ездить"), True)
    check("разговор ушёл модели", brain.asked, ["расскажи про космос"])
    check("попрощался", said[-1], "Ага, зови.")
    # Реплика модели идёт мимо voice.say — потоком, поэтому её в said нет.
    check("после прощания молчит", len(said), 7)

    # --- второй сценарий: шум, чужой разговор и движение через модель ---
    said.clear()
    moved.clear()
    addressed = app.Addressed()
    tools2 = build_tools(ros, timers, addressed=addressed)
    by_name = {t.name: t for t in tools2}

    # Модель, которая пытается поехать, не спросив имени.
    class DrivingBrain(FakeBrain):
        def reply(self, text, on_text):
            self.asked.append(text)
            answer = by_name["drive"]({"direction": "вперёд"})
            on_text(answer)
            said.append(answer)     # чтобы увидеть, что ответил инструмент
            return answer

    phrases = [
        "Кузя который час",        # проснулись по имени
        "...",                     # мусор от Whisper — «не расслышал»
        "...",                     # второй подряд — молчим
        "а я вчера ходил в магазин и купил там очень много всякой всячины",
        "поезжай на кухню",        # без имени, через модель — ехать нельзя
    ]
    app._listen_loop(cfg, FakeListener(phrases), types.SimpleNamespace(
        transcribe=lambda wav: wav.decode()), DrivingBrain(), FakeVoice(),
        tools2, addressed)

    check("на мусор ответил один раз", said.count("Не расслышал."), 1)
    check("длинная чужая фраза не ушла модели",
          any("магазин" in s for s in said), False)
    check("модель без имени не поехала", said[-1],
          "Для поездки позови меня по имени.")
    check("колёса не тронулись", moved, [])


def main() -> int:
    for test in (test_rules, test_numbers, test_when, test_timers,
                 test_alarms, test_survives_restart, test_alarm_rules,
                 test_weather, test_notes, test_unsure_does_not_drive,
                 test_slicing, test_stop_while_thinking,
                 test_speech_streams,
                 test_pc_url, test_hidden,
                 test_thinkless, test_endpoints, test_brain_money,
                 test_history,
                 test_names, test_dialogue):
        test()
        print("   ...")
    if FAILED:
        print(f"\nРАЗОШЛОСЬ: {len(FAILED)}")
        for item in FAILED:
            print(f"  ✗ {item}")
        return 1
    print("\nВсё сошлось.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
