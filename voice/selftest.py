#!/usr/bin/env python3
"""Самопроверка голосового пайплайна — без микрофона, сети и шасси.

Зачем: правила, числа словами и таймеры легко сломать незаметно. Тест гоняет
их на живом коде и печатает по-русски, что именно разошлось.

    cd ~/Robot-AI/voice && python3 selftest.py

Ставить pytest на робота ради этого незачем, поэтому обычный скрипт.
Возвращает 0, если всё сошлось, и 1 если нет, — годится для CI.
"""

from __future__ import annotations

import sys
import tempfile
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
        BadRequestError=type("BadRequestError", (Exception,), {})),
}
for name, make_stub in _STUBS.items():
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = make_stub()

from robot_voice import ru, weather, when                      # noqa: E402
from robot_voice.brain import HISTORY_LIMIT, _trim    # noqa: E402
from robot_voice.intents import parse                 # noqa: E402
from robot_voice.notes import Notes                   # noqa: E402
from robot_voice.tools import Timers, build_tools     # noqa: E402

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
    for path in sorted((here / "robot_voice").glob("*.py")) + [here / "selftest.py"]:
        top = symtable.symtable(path.read_text(), str(path), "exec")
        known = {s.get_name() for s in top.get_symbols()} | set(dir(builtins))
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
                 test_alarms, test_weather, test_notes, test_history,
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
