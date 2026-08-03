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
from robot_voice.music import Player, Pult            # noqa: E402
from robot_voice.notes import Notes                   # noqa: E402
from robot_voice.tools import Timers, build_tools     # noqa: E402
from robot_voice.tts import PhraseCache, WebSpeech    # noqa: E402

FAILED: list[str] = []


class ГлухойПульт(Pult):
    """Пульт, которого нет: всё принимает, ничего не играет.

    Настоящий ходит по HTTP на веб-сервер робота, и в тесте это была бы
    минута таймаутов на пустом месте.
    """

    def __init__(self) -> None:
        super().__init__("http://тест")
        self.команды: list[tuple[str, str]] = []
        self.счётчик = 0

    def _post(self, путь: str, тело: str = "") -> bool:
        self.команды.append((путь, тело))
        return True

    def сыграно(self) -> int | None:
        return self.счётчик


class ФальшивыйТрек:
    """Трек Яндекса ровно в том объёме, в каком его трогает проигрыватель."""

    def __init__(self, номер: int, ссылка: str = "") -> None:
        self.title = f"Песня {номер}"
        self.available = True
        self.track_id = f"{номер}:1"
        self._ссылка = ссылка or f"http://тест/{номер}.mp3"

    def artists_name(self) -> list[str]:
        return ["Кто-то"]


class ФальшивыйЯндекс:
    """Музыка без сети: отдаёт ссылки, считает отзывы станции."""

    possible = True

    def __init__(self, битые: set[str] | None = None) -> None:
        self.битые = битые or set()
        self.партии = 0
        self.отзывы: list[str] = []

    def link(self, track) -> str:
        return "" if track.track_id in self.битые else track._ссылка

    def wave(self, station="user:onyourwave", after=""):
        self.партии += 1
        начало = self.партии * 100
        return [ФальшивыйТрек(начало + i) for i in range(3)], f"партия-{self.партии}"

    def station(self, query: str) -> str:
        return "genre:jazz" if "джаз" in query else ""

    def search(self, query: str, limit: int = 15):
        return [ФальшивыйТрек(i) for i in range(2)]

    def started(self, station, batch="") -> None:
        self.отзывы.append("начали")

    def playing(self, station, track_id, batch="") -> None:
        self.отзывы.append(f"играет {track_id}")

    def played(self, station, track_id, seconds, batch="") -> None:
        self.отзывы.append(f"доиграл {track_id}")


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
        # Новости и город: спрашивают их как попало, поэтому формулировок много.
        ("какие новости", "news"), ("что нового", "news"),
        ("расскажи мне какие новости последние были", "news"),
        ("последние новости", "news"), ("что там нового", "news"),
        ("мы в калининграде", "set_home"),
        ("а живу я в городе калининграде", "set_home"),
        ("наш город калининград", "set_home"),
        # Курс валют и музыка: после погоды и таймера это самое частое.
        ("какой курс доллара", "rates"), ("сколько стоит доллар", "rates"),
        ("включи музыку", "play_music"), ("включи джаз", "play_music"),
        ("поставь радио рекорд", "play_music"),
        ("включи мою волну", "play_music"), ("поставь что-нибудь", "play_music"),
        ("выключи музыку", "stop_music"), ("выключи радио", "stop_music"),
        ("убавь музыку", "music_volume"), ("сделай музыку тише", "music_volume"),
        ("можешь убавить этот звук", "music_volume"),
        ("прибавь громкость", "music_volume"),
        ("следующая песня", "music_next"), ("переключи", "music_next"),
        ("поставь другую песню", "music_next"),
        ("что сейчас играет", "what_is_playing"),
        ("какая это песня", "what_is_playing"),
        ("курс доллара", "rates"), ("почем доллар", "rates"),
        ("какой сегодня курс евро", "rates"), ("курс валют", "rates"),
    ]
    for phrase, tool in cases:
        m = parse(phrase)
        check(f"«{phrase}»", m.tool if m else None, tool)

    section("правила: что должно уходить модели, а не срабатывать")
    for phrase in ["сколько времени в москве", "какая погода в москве",
                   "сколько будет десять процентов от ста",
                   "включи умную зарядку", "поезжай на кухню",
                   # «Включи» — не всегда музыка. Того, что дома включают
                   # кроме радио, у робота нет вовсе, и объяснить это честнее
                   # модели, чем правилу.
                   "включи свет", "включи чайник", "включи телевизор",
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


def test_repair() -> None:
    """Почти расслышанное слово команды чинится по словарю.

    С живого робота: «поставь будильник на 18 часов» приехало как «поставь
    ВОДИЛЬНИК на 18 часов». Правило мимо такого прошло, фраза уехала модели,
    та честно поставила будильник с названием «водильник», и робот объявил
    «Водильник поставлен на восемнадцать часов». Уверенность распознавания
    при этом была хорошая, -0.39, — то есть барьером такое не ловится.

    Приём подсмотрен у Home Assistant с его speech-to-phrase и у Алисы с её
    классификаторами: вопрос не «что человек сказал», а «на какую из фраз,
    которые я умею, это похоже». Мы делаем то же по словам.
    """
    section("починка почти расслышанных команд")
    from robot_voice.intents import repair

    check("водильник", repair("поставь водильник на 18 часов"),
          "поставь будильник на 18 часов")
    check("таймир", repair("поставь таймир на 5 минут"),
          "поставь таймер на 5 минут")
    check("постав", repair("постав будильник"), "поставь будильник")

    m = parse("поставь водильник на 18 часов")
    check("починенное доезжает до правила", m.tool if m else None, "set_alarm")

    # А вот чего чинить нельзя.
    check("короткое слово не трогаем", repair("что там со следом"),
          "что там со следом")
    check("двусмысленное не трогаем", repair("астанови таймер"), "астанови таймер")
    check("чужие слова целы", repair("добавь в список сгущенку"),
          "добавь в список сгущенку")
    # Главное: у «напомни» и «добавь» внутри едет речь человека, и первый
    # заход разбора обязан отработать до всякой починки.
    m = parse("напомни завтра в восемь позвонить в поликлинику")
    check("текст напоминания не тронут", m.args["text"] if m else None,
          "позвонить в поликлинику")
    m = parse("добавь в список сгущенку")
    check("пункт списка не тронут", m.args["item"] if m else None, "сгущенку")


def test_made_up() -> None:
    """Заученные концовки роликов — это не речь, а память Whisper.

    С живого робота за один вечер: «Спасибо за внимание!», «С вами был Игорь
    Негода», причём с приличной уверенностью -0.73 и -0.91. Робот отвечал на
    них вслух — то есть разговаривал с холодильником. Барьером уверенности
    такое не ловится: модель не сомневается, она вспоминает субтитры, на
    которых её учили.
    """
    section("выдумки Whisper на тишине")
    from robot_voice.stt import made_up

    for phrase in ["Спасибо за внимание!", "С вами был Игорь Негода.",
                   "Продолжение следует...", "Субтитры сделал DimaTorzok",
                   "спасибо за внимание"]:
        check(f"«{phrase}» — выдумка", made_up(phrase), True)
    # А это живая речь, и глушить её нельзя.
    for phrase in ["Спасибо!", "Спасибо, Кузя", "Продолжи таймер",
                   "Кузя, вперёд", "с вами всё хорошо?"]:
        check(f"«{phrase}» — речь", made_up(phrase), False)


def test_speakable() -> None:
    """Смайлики голосом не передать, и читать их вслух не надо.

    Модель ставит их охотно. На живом роботе целым предложением вышло одно
    «😄» — Кузя честно попытался его произнести.
    """
    section("картинки не звучат")
    from robot_voice.tts import speakable

    check("один смайлик", speakable("😄"), "")
    check("смайлик в конце", speakable("Пожалуйста! 😊"), "Пожалуйста!")
    check("смайлик посреди", speakable("Всё хорошо 👍 правда"), "Всё хорошо правда")
    check("обычную речь не трогаем", speakable("Привет, Игорь!"), "Привет, Игорь!")
    check("цифры и тире целы", speakable("Сейчас 19:12 — время ужина"),
          "Сейчас 19:12 — время ужина")


def test_people() -> None:
    """Личное дело: робот должен помнить, с кем говорит, и уметь забыть.

    Разделение труда с ПК: слепки голосов там, где считаются, — на
    компьютере. Дела здесь, потому что нужны в разговоре и тогда, когда
    компьютер выключен.
    """
    section("личные дела")
    from robot_voice.people import FACTS_LIMIT, People

    store = Path(tempfile.mkdtemp()) / "люди.json"
    people = People(store)

    check("не узнан — запоминать некому", people.remember("", "любит чай"),
          "Я пока не знаю, кто ты. Скажи «запомни мой голос».")
    check("запомнили", people.remember("Игорь", "любит крепкий чай"), "Запомнил.")
    check("дубль не копим", people.remember("Игорь", "Любит крепкий чай"),
          "Это я уже про тебя знаю.")
    people.met("Игорь")
    check("встречу отметили", people.card("Игорь")["разговоров"], 1)

    check("справка для модели", people.brief("Игорь"),
          "Сейчас с тобой говорит Игорь. Что ты о нём знаешь: любит крепкий чай.")
    check("незнакомцу справки нет", people.brief(""), "")
    check("вслух", people.tell("Игорь"),
          "Про тебя знаю вот что. Любит крепкий чай.")

    check("пережило перезапуск", People(store).card("Игорь")["разговоров"], 1)
    check("стёрли", people.forget("Игорь"), "Всё стёр.")
    check("и правда стёрли", People(store).known(), [])


def test_notes_about_people() -> None:
    """Конспект: робот дописывает дело сам, но поручения этим не вытесняет.

    Человек не диктует роботу анкету — он просто разговаривает. Значит,
    подмечать надо самому. Но у таких заметок два свойства: они врут чаще и
    накапливаются быстрее, поэтому вытесняться должны первыми, а объявлять о
    них вслух не надо вовсе — иначе каждая реплика кончается «записал».
    """
    section("робот ведёт конспект сам")
    from robot_voice.people import FACTS_LIMIT, People

    store = Path(tempfile.mkdtemp()) / "люди.json"
    people = People(store)

    check("подмеченное не объявляется вслух",
          people.remember("Игорь", "работает по ночам", asked=False),
          "Записал в память, вслух не говори.")
    check("по просьбе — объявляется",
          people.remember("Игорь", "любит крепкий чай"), "Запомнил.")
    check("обрывок не записываем",
          people.remember("Игорь", "ага", asked=False), "Слишком коротко, не записал.")
    check("незнакомца не записываем",
          people.remember("", "что-то важное", asked=False),
          "Не знаю, кто говорит — записывать некуда.")

    # Повтор своими словами — не новая запись. Но если формулировка подробнее,
    # берём её: робот вернулся к теме и уточнил.
    check("тот же факт другими словами",
          people.remember("Игорь", "чай крепкий любит", asked=False),
          "Это я уже про тебя знаю.")
    check("подробность дописалась",
          people.remember("Игорь", "любит крепкий чай по утрам", asked=False),
          "Это я уже про тебя знаю.")
    check("и заменила прежнее", "любит крепкий чай по утрам" in people.facts("Игорь"),
          True)
    # А вот это — РАЗНОЕ, хоть и похоже на восемьдесят процентов букв. Слить их
    # в одну запись значит потерять то, ради чего дело и заводилось.
    people.remember("Игорь", "любит кофе", asked=False)
    check("похожее, но другое — отдельно", len(people.facts("Игорь")), 3)

    # Просьба запомнить то, что робот уже подметил сам, превращает заметку в
    # поручение: вытеснять её больше нельзя.
    people.remember("Игорь", "работает по ночам")
    подмеченных = [f for f in people.card("Игорь")["факты"] if f.get("сам")]
    check("просьба закрепила заметку",
          any(f["что"] == "работает по ночам" for f in подмеченных), False)

    # Дело не растёт без предела — оно уезжает в каждый запрос. И вытесняется
    # сначала подмеченное, а не то, что человек просил запомнить.
    for i in range(FACTS_LIMIT + 5):
        people.remember("Игорь", f"подметил мелочь про случай {i}", asked=False)
    check("дело не растёт без предела",
          len(people.card("Игорь")["факты"]), FACTS_LIMIT)
    check("поручение уцелело", "работает по ночам" in people.facts("Игорь"), True)
    check("и уточнённое поручение тоже",
          "любит крепкий чай по утрам" in people.facts("Игорь"), True)
    # А подмеченное «любит кофе» вытеснено — и правильно: это была догадка
    # робота, а не просьба человека.
    check("догадка уступила место", "любит кофе" in people.facts("Игорь"), False)

    # Старый формат — простые строки. Дело человека терять из-за смены формата
    # нельзя: там записи, которые он роботу диктовал.
    store.write_text('{"Настя": {"факты": ["любит гулять"], "разговоров": 3}}', "utf-8")
    старое = People(store)
    check("старое дело читается", старое.facts("Настя"), ["любит гулять"])
    старое.remember("Настя", "учится в институте", asked=False)
    check("и дополняется", старое.facts("Настя"),
          ["любит гулять", "учится в институте"])


def test_meeting() -> None:
    """Знакомство без обряда: имя со слуха и команды про самого человека."""
    section("знакомство")
    from robot_voice.app import _person_name

    for phrase, name in [("меня зовут Игорь", "Игорь"), ("я Игорь", "Игорь"),
                         ("Игорь", "Игорь"), ("это Настя", "Настя"),
                         ("Игорь Петрович", "Игорь")]:
        check(f"«{phrase}»", _person_name(phrase), name)
    # А это не имена. «меНЯ зовут» когда-то давало «Зовут» — границы слов важны.
    for phrase in ["ладно", "не скажу", "а я не скажу тебе имя вовсе никогда", ""]:
        check(f"«{phrase}» — не имя", _person_name(phrase), "")

    # Команды про самого человека разбираются правилами, а не моделью: «забудь
    # про меня» должно стирать дело, а не отвечать «хорошо, забыл». И правила
    # обязаны переживать пунктуацию Whisper: на живом роботе «запомни, мой
    # голос» не сработало, робот спросил модель, и та заявила, что голоса
    # запоминать не умеет.
    from robot_voice.app import (_FORGET_ME, _KNOW_ME, _MY_NAME, _REMEMBER,
                                 _WHAT_ABOUT_ME, _WHO_AM_I, _bare)

    def бьётся(rule, phrase: str) -> bool:
        return bool(rule.match(_bare(phrase)))

    for phrase in ["запомни мой голос", "запомни, мой голос", "запомнишь мой голос",
                   "давай знакомиться", "познакомимся", "узнавай меня"]:
        check(f"«{phrase}» — знакомство", бьётся(_KNOW_ME, phrase), True)
    for phrase in ["кто я", "кто я?", "ты меня узнал?", "узнаешь меня"]:
        check(f"«{phrase}» — кто я", бьётся(_WHO_AM_I, phrase), True)
    for phrase in ["что ты обо мне знаешь?", "что ты помнишь", "— Что ты знаешь обо мне?"]:
        check(f"«{phrase}» — что знаешь", бьётся(_WHAT_ABOUT_ME, phrase), True)
    check("забудь про меня", бьётся(_FORGET_ME, "забудь про меня"), True)
    check("сотри моё дело", бьётся(_FORGET_ME, "сотри моё дело"), True)
    check("запомни, что я люблю чай",
          _REMEMBER.match(_bare("запомни, что я люблю чай")).group(1), "я люблю чай")
    check("меня зовут Игорь", бьётся(_MY_NAME, "меня зовут Игорь"), True)
    # И чего эти правила ловить не должны.
    check("«забудь» одно — это не про меня", бьётся(_FORGET_ME, "забудь"), False)
    check("«кто ты» — не «кто я»", бьётся(_WHO_AM_I, "кто ты"), False)


def test_auto_meeting() -> None:
    """Голос заводится сам, имя приходит потом — и архив едет за ним.

    Обряд знакомства («скажи три фразы») выкинут: на живом роботе он не
    сработал ни разу, а по существу лишний — людям не приходит в голову
    представляться пылесосу. Гость приходит, разговаривает, робот заводит на
    него дело под кличкой и копит заметки. Назвался — кличка стала именем
    вместе со всем архивом. Не назвался — и ладно.
    """
    section("голос заводится сам")
    from robot_voice.app import Meeting
    from robot_voice.people import People

    store = Path(tempfile.mkdtemp()) / "люди.json"
    people = People(store)

    # Гость поговорил, робот записал за ним пару наблюдений — ещё не зная имени.
    people.met("голос 1")
    people.remember("голос 1", "приехал из Москвы", asked=False)
    check("безымянного видно", people.nameless("голос 1"), True)
    check("модель понимает, что имени нет", people.brief("голос 1"),
          "Ты узнаёшь этот голос, но имени не знаешь. "
          "Что ты о нём знаешь: приехал из Москвы.")

    # Назвался — архив переезжает целиком.
    people.rename("голос 1", "Настя")
    check("кличка исчезла", "голос 1" in people.cards, False)
    check("заметки уцелели", people.facts("Настя"), ["приехал из Москвы"])
    check("и счётчик встреч", people.card("Настя")["разговоров"], 1)
    check("теперь с именем", people.nameless("Настя"), False)

    # Если под именем уже было дело, два сливаются, ничего не теряя.
    people.remember("Настя", "любит чай")
    people.met("голос 7")
    people.remember("голос 7", "работает в школе", asked=False)
    people.rename("голос 7", "Настя")
    check("дела слились", sorted(people.facts("Настя")),
          ["любит чай", "приехал из Москвы", "работает в школе"])
    check("встречи сложились", people.card("Настя")["разговоров"], 2)

    # Робот спрашивает имя один раз и не раньше третьего разговора: раньше
    # навязчиво, позже глупо — человек уже всё рассказал.
    meeting = Meeting("")
    people.met("голос 9")
    check("после первого разговора молчим",
          meeting.time_to_ask("голос 9", people), False)
    people.met("голос 9")
    people.met("голос 9")
    check("после третьего спрашиваем",
          meeting.time_to_ask("голос 9", people), True)
    meeting.asking = True
    check("пока спрашиваем — второй раз не лезем",
          meeting.time_to_ask("голос 9", people), False)
    # У кого имя есть, того не переспрашиваем никогда.
    for _ in range(5):
        people.met("Настя")
    check("названного не переспрашиваем",
          Meeting("").time_to_ask("Настя", people), False)


def test_ascii_out() -> None:
    """В заголовках и адресах запросов не должно быть кириллицы.

    Дважды на одни грабли. Сначала имя человека в адресе — «?имя=Игорь», — и
    знакомство падало молча: робот говорил «не вышло запомнить голос», а
    запрос не уходил вовсе. Потом кириллица в User-Agent, и курс валют
    отвечал «сервис не ответил», хотя до сервиса дело не доходило.

    Причина одна: строку запроса и заголовки HTTP кодирует в latin-1, и любая
    русская буква роняет отправку. Ошибка при этом маскируется под отказ
    чужого сервиса — то есть ищут её не там.
    """
    section("наружу — только латиница")
    import ast
    import re

    here = Path(__file__).resolve().parent
    files = sorted((here / "robot_voice").glob("*.py"))
    плохие: list[str] = []
    for path in files:
        дерево = ast.parse(path.read_text(), str(path))
        # Постоянные модуля: значение заголовка чаще пишут через имя, а не
        # строкой на месте. Ровно так и было с User-Agent, из-за чего первая
        # версия этой проверки ничего не нашла.
        постоянные = {
            цель.id: узел.value.value
            for узел in дерево.body
            if isinstance(узел, ast.Assign) and isinstance(узел.value, ast.Constant)
            and isinstance(узел.value.value, str)
            for цель in узел.targets if isinstance(цель, ast.Name)
        }

        def строкой(узел):
            """Значение узла, если это строка или имя известной постоянной."""
            if isinstance(узел, ast.Constant) and isinstance(узел.value, str):
                return узел.value
            if isinstance(узел, ast.Name):
                return постоянные.get(узел.id)
            return None

        for узел in ast.walk(дерево):
            # Заголовки: {"User-Agent": ...} и любые другие пары в headers.
            if isinstance(узел, ast.Dict):
                for ключ, значение in zip(узел.keys, узел.values):
                    имя = строкой(ключ)
                    если = строкой(значение)
                    if имя is None or если is None:
                        continue
                    if "-" not in имя or " " in имя:
                        continue        # это не заголовок, а обычный словарь
                    пара = f"{имя}: {если}"
                    if not пара.isascii():
                        плохие.append(f"{path.name}: заголовок {пара!r}")
            # Куски адресов: «?имя=» и «&name=». Регулярки и строки документации
            # сюда попадать не должны, поэтому требуем именно вид параметра
            # сразу за знаком вопроса и запрещаем всё, чем пишут шаблоны.
            if isinstance(узел, ast.Constant) and isinstance(узел.value, str):
                текст = узел.value
                похоже = re.search(r"[?&][^\s?&=]+=", текст)
                шаблон = "(?" in текст or "\\" in текст or len(текст) > 200
                if похоже and not шаблон and not текст.isascii():
                    плохие.append(f"{path.name}: адрес {текст!r}")
    check("кириллицы в заголовках и адресах нет", sorted(set(плохие)), [])


def test_wake_word() -> None:
    """Имя робота Whisper пишет как попало — ловить надо все варианты.

    С живого робота за один вечер: «Хузя», «Куля», «Куся», «Культа». Половина
    обращений проходила мимо, и человек повторял всё громче. Порог измерен на
    этих самых вариантах: они дают 0.75, а обычные слова — не выше 0.67.
    Между ними чистый зазор, по нему и режем.
    """
    section("варианты имени")
    from robot_voice.app import _strip_wake_word

    имена = ("кузя", "кузь", "куся", "кузи", "кузьма")
    порог = Config().wake_ratio
    for phrase in ["Кузя, привет", "Хузя, привет", "Куля, привет",
                   "Куся, привет", "Гузя, привет"]:
        check(f"«{phrase}» — это ко мне",
              _strip_wake_word(phrase, имена, порог), "привет")
    # А это обычные слова, и робот не должен на них просыпаться.
    for phrase in ["куда мне идти", "коля пришёл", "кухня большая",
                   "кузов машины", "пуля просвистела", "муза пришла"]:
        check(f"«{phrase}» — не имя", _strip_wake_word(phrase, имена, порог), None)


def test_not_for_me() -> None:
    """В открытом окне робот не должен вступать в чужой разговор.

    С живого робота: после «Кузя, привет» окно оставалось открытым двадцать
    секунд, и в него попало «Иди уже, Рома! Рома, пиши!» и «Виктор, как дела
    у тебя?» — телевизор и разговор с другим человеком. Робот честно ответил
    обоим. Отличить чужую речь от своей по звуку мы не умеем, но обращение по
    чужому имени — признак надёжный и дешёвый.
    """
    section("зовут не меня")
    from robot_voice.app import _NOT_A_NAME, _VOCATIVE, _clean_token

    def чужое(text: str) -> bool:
        m = _VOCATIVE.match(text.strip())
        return bool(m) and _clean_token(m.group(1)) not in _NOT_A_NAME

    for phrase in ["Виктор, как дела у тебя?", "Рома, пиши!",
                   "Наташа, ты идёшь?", "Серёжа, где ключи"]:
        check(f"«{phrase}» — не мне", чужое(phrase), True)
    # А это фразы, которые начинаются похоже, но обращены к роботу.
    for phrase in ["Ладно, поехали", "Хорошо, поставь таймер", "Так, что там",
                   "Стоп, стоп", "Погоди, не надо", "Спасибо, всё",
                   "поставь таймер на пять минут", "Сколько времени?"]:
        check(f"«{phrase}» — мне", чужое(phrase), False)


def test_remote_voice() -> None:
    """Голос берём с ПК, но немым от его выключения не становимся.

    Синтез уехал на ПК ради двух вещей: там silero вместо piper — с
    ударениями, омографами и вопросительной интонацией, — и там он считается
    в разы быстрее, чем на Cortex-A55 робота. Но ПК выключают, и тогда робот
    обязан договорить своим голосом, а не замолчать посреди фразы.
    """
    section("голос с ПК и отступление на свой")
    import http.server
    import io
    import threading as thr
    import wave

    from robot_voice.tts import RemoteVoice, _as_wav

    просили: list[dict] = []

    class Ответчик(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)))
            просили.append(body)
            if body.get("text") == "молчу":
                self.send_response(500)
                self.end_headers()
                return
            wav = _as_wav(b"\x00\x01" * 240, 24000)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ответчик)
    thr.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        store = Path(tempfile.mkdtemp())
        voice = RemoteVoice(url, "eugene", PhraseCache(store, "пк"))
        pcm = voice.raw("Привет!")
        check("звук пришёл", len(pcm), 480)
        check("частота с ПК", voice.rate, 24000)
        check("голос попросили", просили[-1], {"text": "Привет!", "voice": "eugene"})

        # Второй раз ту же фразу у ПК не спрашиваем: она уже на диске.
        было = len(просили)
        check("из кэша", voice.raw("Привет!"), pcm)
        check("ПК не тревожили", len(просили), было)

        # ПК ответил отказом — робот отступает и час туда не ходит, иначе
        # каждая фраза начиналась бы с ожидания мёртвого сервера.
        check("отказ — говорим сами", voice.raw("молчу"), None)
        check("и больше не стучимся", voice.alive(), False)

        # Выключенный ПК — то же самое, но без ответа вовсе.
        мёртвый = RemoteVoice("http://127.0.0.1:1", "eugene")
        check("выключенный ПК", мёртвый.raw("Привет!"), None)

        # Заголовок, который мы шлём пульту, должен быть настоящим wav.
        with wave.open(io.BytesIO(_as_wav(pcm, voice.rate))) as w:
            check("пульту уходит верная частота", w.getframerate(), 24000)
    finally:
        srv.shutdown()


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


def прогон_фраз(схема, шумно: bool = False) -> list[float]:
    """Схема — список (речь?, сколько кадров). Возвращает длины фраз.

    Живой Listener без микрофона: детектор речи подменён заранее известной
    последовательностью ответов, а кадры — тишиной нужной длины.
    """
    import threading as th
    from collections import deque

    import numpy as np

    from robot_voice import audio

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
    l.silence_frames, l._min_speech = 35, 15            # 700 мс, 300 мс
    l.max_speech_frames = 1000                          # 20 с
    l._start, l.preroll = 2, deque(maxlen=15)
    l._noisy = шумно
    l._muted = th.Event()
    return [round((len(w) - 44) / 2 / 16000, 1) for w in l.utterances()]


def test_slicing() -> None:
    """Нарезка на фразы: что уезжает в распознавание, а что нет.

    Два порога тут не работали вовсе. Одиночный щелчок проходил как секунда
    почти-тишины, потому что «речевыми» считались и предбуфер, и семьсот
    миллисекунд тишины на конце. А непрерывный шум длиннее двадцати секунд
    не отбрасывался, а отдавался как обычная фраза — и робот замолкал на
    минуту, потому что распознавание идёт втрое дольше самого звука.
    """
    section("нарезка на фразы")
    прогон = прогон_фраз

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
                        notes=Notes(store / "notes.json"),
                        set_place=lambda *a: None, player=Player(ГлухойПульт()))
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

    # Тот самый случай с живого робота. Первая версия фильтра отпускала начало
    # через триста символов, решив, что размышлений нет, — а их было полторы
    # тысячи, и робот зачитал вслух все. Порога быть не должно вовсе.
    ворох = "Надо ответить коротко. Хотя, возможно, лучше переспросить. " * 30
    check("полторы страницы размышлений не звучат",
          run([ворох, "</think>", "Привет!"]), "Привет!")


def test_thinkless_habit() -> None:
    """Фильтр размышлений должен учиться, иначе он либо режет, либо тормозит.

    Держать начало ответа до </think> — единственный надёжный способ не
    зачитать размышления вслух, но за него платят задержкой: пока тега нет,
    робот молчит. Платить каждую реплику незачем — думает модель вслух или
    нет, это её свойство, а не свойство реплики. Выяснили один раз и дальше
    отдаём поток сразу.
    """
    section("фильтр размышлений учится на первом ответе")

    def run(habit, chunks: list[str]):
        """Возвращает сказанное и то, что прозвучало ДО конца ответа."""
        said: list[str] = []
        f = Thinkless(said.append, habit)
        for c in chunks:
            f.feed(c)
        early = "".join(said)
        f.close()
        return "".join(said), early

    ep = types.SimpleNamespace(name="ПК", thinks=None)
    said, _ = run(ep, ["думаю вслух", "</think>", "Привет!"])
    check("болтун пойман", said, "Привет!")
    check("привычка запомнена", ep.thinks, True)
    # Теперь ждём тег сколько угодно — и до конца ответа не говорим ни слова.
    said, early = run(ep, ["очень длинные размышления " * 50, "</think>", "Да."])
    check("известного болтуна держим до тега", said, "Да.")

    ep = types.SimpleNamespace(name="облако", thinks=None)
    said, early = run(ep, ["Привет, ", "Игорь!"])
    check("молчун распознан", ep.thinks, False)
    check("первый ответ придержан целиком", early, "")
    # А второй уже идёт потоком: робот начинает говорить с первого куска.
    said, early = run(ep, ["Привет, ", "Игорь!"])
    check("дальше говорим сразу", early, "Привет, Игорь!")

    # Модель, которая раньше не думала, вдруг задумалась. Начало уже
    # прозвучало — не вернуть, но остаток размышлений звучать не должен.
    said, _ = run(ep, ["ага, ", "надо подумать", "</think>", "Готово."])
    check("передумавшая модель обрезана", said, "ага, надо подумать" + "Готово.")
    check("привычка пересмотрена", ep.thinks, True)

    # Обратно — не переучиваемся. «Думает» доказано тегом, «не думает» — всего
    # лишь его отсутствием, и один такой ответ ничего не отменяет: иначе
    # фильтр перестанет держать начало и следующие размышления пойдут в речь.
    said, early = run(ep, ["Привет!"])
    check("один ответ без тега не отменяет доказанного", ep.thinks, True)
    check("и держать не перестаём", early, "")


def test_smart() -> None:
    """«Подумай хорошо» уводит ход в облако, а обычная фраза — нет.

    Зоопарк моделей в шесть гигабайт видеопамяти не помещается: пока одна
    отвечает, остальные пришлось бы выгружать, и каждая смена стоила бы
    десятки секунд. Вместо зоопарка — лестница: правила, домашняя модель,
    облако. Не хватило домашней — человек зовёт умного вслух и платит
    осознанно, а не по решению классификатора, который сам ошибается.
    """
    section("позвать умного по просьбе")
    from robot_voice.intents import wants_smart

    check("подумай хорошо", wants_smart("подумай хорошо сколько будет 17 на 23"),
          (True, "сколько будет 17 на 23"))
    check("спроси умного", wants_smart("спроси умного как работает меканум"),
          (True, "как работает меканум"))
    check("напрягись", wants_smart("напрягись, что подарить маме")[0], True)
    check("обычная фраза не трогается",
          wants_smart("расскажи анекдот"), (False, "расскажи анекдот"))
    # «Подумай» в одиночку — продолжение разговора, а не пустой вопрос:
    # спрашивать умного не о чем, но и терять фразу нельзя.
    check("одна просьба без вопроса", wants_smart("подумай"), (True, "подумай"))

    # И сама развилка: при просьбе первым спрашивается платный.
    cfg = Config()
    cfg.local_api_base = "http://пк:4000"
    cfg.api_key = "ключ"
    b = Brain(cfg, [])
    check("обычно первым идёт ПК", [e.paid for e in b._live()], [False, True])
    check("умного просили — первым облако",
          [e.paid for e in b._live(smart=True)], [True, False])


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

    def сбой(messages, on_text, smart=False):
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
    b2._round = lambda messages, on_text, smart=False: (
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
        def reply(self, text, on_text, smart=False):
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
        def reply(self, text, on_text, smart=False):
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


def test_music_queue() -> None:
    """Очередь треков: кончился — ставим следующий, битый — пропускаем.

    Обратного канала от пульта к роботу нет, есть счётчик доигранных песен.
    Всё, что может пойти не так, идёт не так именно вокруг него: ссылка не
    открылась, пульт сообщил «доиграл» раньше, чем мы успели запомнить, от
    чего считать, — и музыка молча вставала на первой же песне.
    """
    section("очередь треков")
    пульт, ya = ГлухойПульт(), ФальшивыйЯндекс()
    player = Player(пульт, ya)

    треки = [ФальшивыйТрек(i) for i in range(3)]
    check("первый трек назван", player.очередь(треки, "genre:jazz", "п1"),
          "Кто-то — Песня 0")
    check("станции сообщили, что включились", ya.отзывы[0], "начали")
    check("пульт получил трек",
          [п for п, _ in пульт.команды if п == "/speak/track"], ["/speak/track"])

    # Пульт доиграл песню — счётчик вырос, робот обязан поставить следующую.
    пульт.счётчик += 1
    player._tick()
    check("после конца играет следующий", player.название, "Кто-то — Песня 1")
    check("ротору сказали, что трек дослушали",
          any(о.startswith("доиграл") for о in ya.отзывы), True)

    # Битая ссылка не должна останавливать вечер.
    плохой = ФальшивыйЯндекс(битые={"7:1"})
    player2 = Player(ГлухойПульт(), плохой)
    check("битый трек пропущен",
          player2.очередь([ФальшивыйТрек(7), ФальшивыйТрек(8)]),
          "Кто-то — Песня 8")

    # Отметку счётчика надо брать ДО отправки трека: иначе мгновенная ошибка
    # проигрывания принимается за исходное значение и следующая песня
    # не наступает никогда.
    п3 = ГлухойПульт()
    п3.счётчик = 5
    player3 = Player(п3, ФальшивыйЯндекс())
    player3.очередь([ФальшивыйТрек(1), ФальшивыйТрек(2)])
    п3.счётчик = 6                       # пульт сразу сказал «не пошло»
    player3._tick()
    check("мгновенная ошибка не останавливает очередь",
          player3.название, "Кто-то — Песня 2")

    # Станция досыпает треков, пока очередь не опустела.
    п4 = ГлухойПульт()
    ya4 = ФальшивыйЯндекс()
    player4 = Player(п4, ya4)
    player4.очередь([ФальшивыйТрек(1)], "user:onyourwave", "п1")
    было = ya4.партии
    for _ in range(4):          # доигрываем всё, что станция уже дала
        п4.счётчик += 1
        player4._tick()
    check("станция подсыпала треков", ya4.партии > было, True)
    check("музыка не кончилась", player4.играет, True)

    # Веб-сервер перезапустили — счётчик начал считать с нуля. Без этого его
    # ноль никогда не стал бы больше нашей семёрки, и музыка встала бы навсегда.
    п6 = ГлухойПульт()
    п6.счётчик = 7
    player6 = Player(п6, ФальшивыйЯндекс())
    player6.очередь([ФальшивыйТрек(1), ФальшивыйТрек(2)])
    п6.счётчик = 0                       # сервер перезапустился
    player6._tick()
    check("перезапуск сервера не двигает трек", player6.название, "Кто-то — Песня 1")
    п6.счётчик = 1                       # а вот теперь песня и правда кончилась
    player6._tick()
    check("после перезапуска очередь снова идёт",
          player6.название, "Кто-то — Песня 2")

    # Вкладку закрыли — «доиграл» не придёт никогда. Без запаса по времени
    # музыка встала бы намертво до следующей команды голосом.
    п5 = ГлухойПульт()
    player5 = Player(п5, ФальшивыйЯндекс())
    player5.очередь([ФальшивыйТрек(1), ФальшивыйТрек(2)])
    player5._потолок = 0.0                # как будто песня давно кончилась
    player5._tick()
    check("молчащий пульт не вешает музыку", player5.название, "Кто-то — Песня 2")

    check("выключил — не играет", player.выключить(), "Выключил.")
    check("выключать нечего", player.выключить(), "Музыка и так не играет.")


def test_music_volume() -> None:
    """Громкость музыки — отдельно от громкости голоса.

    «Кузя, убавь музыку» на живом роботе кончилось тем, что он её выключил:
    правило про громкость знало только про голос, а под «убавь» подошло
    выключение. Теперь решает инструмент — по тому, играет ли что-нибудь.
    """
    section("громкость музыки")
    store = Path(tempfile.mkdtemp())
    timers = Timers(announce=lambda text, **kw: None, store=store / "timers.json")
    ros = types.SimpleNamespace(voltage=12.4, moving=False, connected=True,
                                drive=lambda *a, **k: None, stop_motion=lambda: None)
    громкость = {"голос": 1.0}
    speaker = types.SimpleNamespace(
        volume=1.0, last_said="", hush=lambda: None,
        set_volume=lambda v: громкость.__setitem__("голос", v))
    пульт = ГлухойПульт()
    player = Player(пульт, ФальшивыйЯндекс())
    tools = {t.name: t for t in build_tools(ros, timers, speaker=speaker,
                                            player=player)}

    # Музыка молчит — «тише» относится к голосу робота.
    tools["music_volume"]({"изменение": "тише"})
    check("без музыки тише говорит робот", громкость["голос"] < 1.0, True)

    # «Включи радио» — это радио, а не Яндекс. Без этого просьба про радио
    # уезжала в «Мою волну», а «включи радио Рекорд» искало в Яндексе песни
    # со словом «Рекорд» в названии.
    # Не нашлось — значит не нашлось. На живом роботе «включи музыку Грот»
    # без Яндекса кончилось тем, что модель подставила сначала «Группа крови»,
    # потом «джаз», и человеку заиграл джаз вместо того, что он просил.
    без_яндекса = Player(ГлухойПульт())          # только радио, как у Игоря
    сам = {t.name: t for t in build_tools(ros, timers, speaker=speaker,
                                          player=без_яндекса)}
    from robot_voice import radio as radio_api
    было_find = radio_api.find
    radio_api.find = lambda что, **kw: None
    try:
        отказ = сам["play_music"]({"что": "Грот"})
        check("отказ называет причину", "Яндекс" in отказ, True)
        check("отказ запрещает подбирать замену", "НЕ подбирай" in отказ, True)
        check("ничего не заиграло", без_яндекса.играет, False)
    finally:
        radio_api.find = было_find

    # По названию правилами, не моделью. На живом роботе «включи группу
    # Краски» уходило модели, а та, наученная прошлыми отказами, отвечала «не
    # найдено в моих источниках», ни разу не вызвав инструмент: Яндекс был
    # подключён и работал, волна играла, а по названию — ничего.
    for фраза, ждём in (("включи группу краски", "краски"),
                        ("включи группу король и шут", "король и шут"),
                        ("включи грота", "грота"),
                        ("поставь короля и шута", "короля и шута"),
                        ("включи альбом кино", "кино"),
                        ("включи песню грот", "грот")):
        м = parse(фраза)
        check(f"«{фраза}» разбирается правилом",
              (м.tool if м else None, (м.args.get("что") if м else None)),
              ("play_music", ждём))

    # И не хватает лишнего: всё это дома включают руками, музыки тут нет.
    for фраза in ("включи свет", "включи телевизор", "включи фонарик",
                  "включи камеру", "поставь чайник", "включи вентилятор",
                  "включи свет в зале", "включи громче", "включи тише"):
        м = parse(фраза)
        check(f"«{фраза}» — не музыка",
              м.tool if м else None,
              None if "громче" not in фраза and "тише" not in фраза else "volume")

    m = parse("включи радио рекорд")
    check("радио названо явно", m.args.get("источник"), "радио")
    radio_api.find = lambda что, **kw: ("Рекорд", "http://тест/поток.mp3")
    try:
        check("по слову «радио» идём в радио, а не в Яндекс",
              tools["play_music"]({"что": "рекорд", "источник": "радио"}),
              "Включаю Рекорд.")
        check("играет поток, а не очередь треков", player.режим, "поток")
    finally:
        radio_api.find = было_find

    player.очередь([ФальшивыйТрек(1)])
    было = player.громкость_
    check("с музыкой тише становится музыка",
          tools["music_volume"]({"изменение": "тише"}), "Сделал тише, 6 из 10.")
    check("голос не тронут", player.громкость_ < было, True)
    check("пульту сказали новую громкость",
          any(п == "/speak/volume" for п, _ in пульт.команды), True)

    for _ in range(10):
        player.тише()
    check("тише бесконечно нельзя", player.тише(),
          "Тише уже некуда. Сейчас 1 из 10.")
    for _ in range(10):
        player.громче()
    check("громче бесконечно нельзя", player.громче(),
          "Громче уже некуда. Сейчас 10 из 10.")

    # Десятибалльная шкала: «убавь до двух» человек говорит, «ноль шестьдесят
    # пять» — нет. Раньше числа передать было нечем, и модель отвечала «звук
    # уменьшен до двух», не тронув громкость.
    check("ступень числом", tools["music_volume"]({"изменение": "тише",
                                                   "уровень": 3}),
          "Сделал тише, 3 из 10.")
    check("доля пересчиталась", player.громкость_, 0.3)
    check("выше десяти не пускаем",
          tools["music_volume"]({"изменение": "громче", "уровень": 99}),
          "Сделал громче, 10 из 10.")
    check("ниже единицы не пускаем",
          tools["music_volume"]({"изменение": "тише", "уровень": 0 - 5}),
          "Сделал тише, 1 из 10.")
    player.на_ступень(8)
    check("«на два» — это подвинуть, а не поставить",
          tools["music_volume"]({"изменение": "тише", "шаг": 2}),
          "Сделал тише, 6 из 10.")
    check("«до двух» — это поставить",
          tools["music_volume"]({"изменение": "тише", "уровень": 2}),
          "Сделал тише, 2 из 10.")

    # Под музыку голое «сделай тише» — про музыку. Свой голос робот и так
    # приглушает, пока играет песня, а человек имеет в виду песню.
    player.на_ступень(7)
    check("«сделай тише» под музыку — про музыку",
          tools["volume"]({"change": "тише"}), "Сделал тише, 6 из 10.")
    check("«говори тише» — всё-таки про голос",
          tools["volume"]({"change": "тише", "про_голос": True}),
          "Хорошо, буду тише.")

    check("что играет", tools["what_is_playing"]({}), "Играет Кто-то — Песня 1.")


def test_noisy_ear() -> None:
    """Под музыку робот должен быть придирчивее к тому, что считать фразой.

    На живом роботе включённое радио ломало распознавание: детектор речи
    принимал музыку за речь, каждая пауза уезжала на ПК отдельным куском и
    возвращалась пустой. Уверенность на настоящих фразах падала с −0.25 до
    −0.75, и «Кузя» превращался в «Кульзу».
    """
    section("шум под музыку")
    from robot_voice import audio

    l = audio.Listener.__new__(audio.Listener)
    l._start, l._min_speech, l._noisy = 2, 15, False
    check("в тишине порог обычный",
          (l.start_frames, l.min_speech_frames), (2, 15))
    l.background(True)
    check("под музыку начать труднее", l.start_frames, 4)
    # А вот длину фразы поднимать нельзя: «стоп» — это меньше полусекунды
    # речи, и удвоенный порог выбросил бы ровно ту команду, ради которой всё
    # и останавливается. Музыкальный мусор в логе был длинный, по три секунды,
    # так что пользы от этого порога всё равно не было бы.
    check("длину фразы музыка не трогает", l.min_speech_frames, 15)
    l.background(False)
    check("музыку выключили — порог вернулся",
          (l.start_frames, l.min_speech_frames), (2, 15))

    # Одиночный щелчок под музыку начинать запись не должен, а короткое
    # «стоп» — должно, и под музыку тоже.
    check("щелчок под музыку не начинает запись",
          прогон_фраз([(False, 20), (True, 3), (False, 40)], шумно=True), [])
    check("короткое «стоп» под музыку слышно",
          len(прогон_фраз([(False, 20), (True, 20), (False, 40)], шумно=True)), 1)


def test_rules_reach_tools() -> None:
    """Каждое правило должно попадать в существующий инструмент.

    Имя из Match ищется среди Tool по строке, и опечатка тут не падает, а
    молча уводит фразу платной модели: правило сработало, инструмента нет,
    в логе — обычное «правилами не разобрал». Ровно так и вышло бы при
    переименовании play_radio → play_music, если забыть одну из двух сторон.
    """
    import re as _re

    from robot_voice.people import People

    section("правила достают до инструментов")
    store = Path(tempfile.mkdtemp())
    timers = Timers(announce=lambda text, **kw: None, store=store / "timers.json")
    ros = types.SimpleNamespace(voltage=12.4, moving=False, connected=True,
                                drive=lambda *a, **k: None, stop_motion=lambda: None)
    speaker = types.SimpleNamespace(volume=1.0, last_said="",
                                    set_volume=lambda v: None, hush=lambda: None)
    # Собираем робота во всей полноте: часть инструментов появляется только
    # вместе с пультом, списком или людьми, и без них проверка бы соврала.
    tools = build_tools(ros, timers, speaker=speaker,
                        notes=Notes(store / "notes.json"),
                        people=People(store / "люди.json"), who=lambda: "Игорь",
                        home=lambda: (54.7, 20.5), set_place=lambda *a: None,
                        addressed=lambda: True, player=Player(ГлухойПульт()))
    есть = {t.name for t in tools}

    исходник = (Path(__file__).resolve().parent / "robot_voice" / "intents.py"
                ).read_text(encoding="utf-8")
    зовут = set(_re.findall(r"Match\(\s*\"([a-z_]+)\"", исходник))
    check("все имена из правил существуют", sorted(зовут - есть), [])


def test_pult_and_server_agree() -> None:
    """Сервер умеет, а страница нет — этот класс ошибок стоил целого вечера.

    Радио тогда не заиграло трижды подряд: сервер уже знал про поток, пульт
    ещё нет; потом браузер держал вчерашнюю страницу; потом кодек оказался не
    тот. Проверка дешёвая: все события, которые сервер рассылает, должны
    разбираться в пульте, а все адреса, куда стучится пульт, — существовать
    на сервере.
    """
    import re as _re

    section("пульт и сервер договорились")
    web = Path(__file__).resolve().parent.parent / "web"
    сервер = (web / "server.py").read_text(encoding="utf-8")
    пульт = (web / "pult.html").read_text(encoding="utf-8")

    события = set(_re.findall(r"SPEECH\.broadcast\(\{\"([^\"]+)\"", сервер))
    незнакомые = sorted(k for k in события
                        if f"data.{k}" not in пульт and f"data['{k}']" not in пульт)
    check("каждое событие сервера разбирается пультом", незнакомые, [])

    адреса = set(_re.findall(r"fetch\('(/[a-z/]+)'", пульт))
    несуществующие = sorted(a for a in адреса if f'"{a}"' not in сервер)
    check("каждый адрес пульта есть на сервере", несуществующие, [])


def main() -> int:
    for test in (test_rules, test_numbers, test_when, test_timers,
                 test_alarms, test_survives_restart, test_alarm_rules,
                 test_weather, test_notes, test_repair, test_made_up, test_speakable, test_people, test_notes_about_people, test_meeting, test_auto_meeting, test_ascii_out, test_wake_word, test_not_for_me, test_remote_voice,
                 test_unsure_does_not_drive,
                 test_slicing, test_stop_while_thinking,
                 test_speech_streams,
                 test_pc_url, test_hidden,
                 test_music_queue, test_music_volume, test_noisy_ear,
                 test_rules_reach_tools,
                 test_pult_and_server_agree,
                 test_thinkless, test_thinkless_habit,
                 test_smart, test_endpoints, test_brain_money,
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
