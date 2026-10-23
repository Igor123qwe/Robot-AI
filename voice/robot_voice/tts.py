"""Голос робота — синтез на домашнем ПК. Куда звук выводить, решает audio_out.

  local   — в свой динамик через aplay: звук ПК приходит по предложениям и
            начинает звучать, не дожидаясь, пока модель допишет ответ.
  browser — реплика собирается в WAV и уходит в веб-сервер робота, а играет её
            браузер с открытым пультом.

Своего синтеза у робота нет (раньше был piper): робот работает только в паре
с ПК, решение хозяина, октябрь 2026. ПК не отвечает — робот молчит.

Реплика синтезируется по предложениям и уходит по мере готовности: две-три
секунды синтеза не ждут своей очереди за десятью секундами генерации. Частые
фразы правил («Не расслышал.», «Остановился.», «Да?») ПК озвучивает один раз,
дальше они берутся из кэша на роботе — пока ПК жив (см. RemoteVoice.raw).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

# Запас на дорогу до браузера и запуск воспроизведения. Реплика уходит по
# сети, вкладка её скачивает и только потом начинает играть — без запаса
# микрофон включится под конец фразы и робот расслышит сам себя.
BROWSER_LAG = 0.8

# Конец предложения: точка/вопрос/восклицание, за которыми пробел или конец строки.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# Смайлики и прочие картинки. Модель ставит их охотно, а синтез читает их
# вслух — на живом роботе целым предложением выходило одно «😄», и Кузя
# честно пытался его произнести. В голосе картинке делать нечего.
_PICTURES = re.compile(
    "[\U0001F000-\U0001FAFF←-⇿⌀-➿️‍⬀-⯿]+")


def speakable(text: str) -> str:
    """Готовит текст к произнесению: убирает то, что голосом не передать.

    Цифры превращаются в слова ИМЕННО ЗДЕСЬ, на последней остановке перед
    синтезом, и это единственное место, где такая правка работает.

    Инструменты и так проговаривают числа словами: погода отдаёт
    «шестнадцать градусов», батарея — «двенадцать и четыре вольта». Но между
    инструментом и динамиком стоит МОДЕЛЬ, и она пересказывает результат
    своими словами:

        вызываю weather({'day': 'сейчас'})
        робот: В Калининграде сейчас 16 градусов, ясно, ветер 11 м/с.

    Аккуратный текст превратился в цифры и сокращения, синтез на них
    спотыкается, и человек слышит пропущенную цифру. То же во всяком свободном
    тексте модели: «около 0,2–0,4 метра от камеры». Уговаривать её промптом
    бесполезно — она пишет как пишет, и проверять каждое слово нечем.
    """
    from . import ru

    чисто = re.sub(r"\s+", " ", _PICTURES.sub(" ", text)).strip()
    return ru.числа_вслух(чисто)


def scale(raw: bytes, volume: float) -> bytes:
    """Меняет громкость сырого звука. Piper своей регулировки не имеет.

    Ночью робот должен говорить тише, а не будить квартиру объявлением
    таймера. Это же используется командой «говори тише».
    """
    if volume >= 0.999:
        return raw
    volume = max(0.0, min(1.0, volume))
    samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
    return (samples * volume).astype("<i2").tobytes()


def пересчитать_частоту(raw: bytes, из: int, в: int) -> bytes:
    """Моно S16 с частоты `из` на частоту `в`. Линейная интерполяция.

    ЗАЧЕМ. aplay открывается один раз на реплику, с частотой ПК на тот миг,
    а частота может смениться посреди реплики (на ПК перезапустили сервер
    с другим голосом). Звук на чужой частоте в той же трубе — голос выше и
    быстрее, и рот на экране (on_pcm) получает не ту частоту. Когда-то так
    звучал запасной piper (22050 против 24000 у ПК): «на второй фразе Кузя
    вдруг зачирикал».

    Линейной интерполяции для речи в динамик хватает. Ни scipy, ни audioop
    (в 3.13 его нет) — только numpy, который здесь и так есть.
    """
    if из == в or not raw or из <= 0 or в <= 0:
        return raw
    отсчёты = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float64)
    if len(отсчёты) < 2:
        return raw
    сколько = int(round(len(отсчёты) * в / из))
    if сколько <= 0:
        return b""
    x_новые = np.arange(сколько) * (из / в)
    x_старые = np.arange(len(отсчёты))
    пересчитано = np.interp(x_новые, x_старые, отсчёты)
    return np.clip(np.rint(пересчитано), -32768, 32767).astype("<i2").tobytes()


class PhraseCache:
    """Готовый звук для фраз, которые робот говорит снова и снова.

    Правила отвечают фиксированными словами: «Да?», «Не расслышал.»,
    «Остановился.», рассказ про умения. Синтезировать их заново каждый раз —
    это порядка секунды на самом частом ответе, и платится она там, где текст
    был известен ещё до того, как человек договорил.

    Кэш дисковый, потому что робот перезапускается каждые две минуты, если в
    репозитории что-то поменялось, — память такого не переживает.
    """

    LIMIT = 200

    def __init__(self, folder: Path, voice: str) -> None:
        self.folder = folder
        self.voice = voice
        self._lock = threading.Lock()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("кэш фраз недоступен (%s) — буду синтезировать каждый раз", e)
            self.folder = None

    def _path(self, text: str) -> Path | None:
        if self.folder is None:
            return None
        key = hashlib.sha1(f"{self.voice}\n{text}".encode("utf-8")).hexdigest()[:16]
        return self.folder / f"{key}.raw"

    def get(self, text: str) -> bytes | None:
        path = self._path(text)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def put(self, text: str, raw: bytes) -> None:
        path = self._path(text)
        # Длинные ответы модели не повторяются, и складывать их — только
        # засорять диск. Кэш имеет смысл ровно для коротких заготовок.
        if path is None or not raw or len(text) > 120:
            return
        with self._lock:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(path)
                self._trim()
            except OSError as e:
                log.debug("не смог сохранить фразу в кэш: %s", e)

    def _trim(self) -> None:
        files = sorted(self.folder.glob("*.raw"), key=lambda p: p.stat().st_mtime)
        for old in files[:-self.LIMIT]:
            try:
                old.unlink()
            except OSError:
                pass


PC_CONNECT = 2.0
# Фраза на ПК синтезируется за доли секунды. Дольше восьми не ждём: всё это
# время голос робота занят, а микрофон приглушён, — и полуживой ПК делал бы
# робота глухим на двадцать секунд на каждой фразе.
PC_READ = 8.0
# Насколько отворачиваемся от ПК, когда он не озвучил: лесенкой, первая
# осечка — пять секунд, дальше вдвое, до минуты. Своего голоса у робота нет
# (робот работает только в паре с ПК), поэтому это не «сколько говорить
# чужим голосом», а «сколько не стучаться в выключенный компьютер».
PC_DOWN_FIRST = 5.0
PC_DOWN = 60.0

# Как часто переспрашивать ПК, каким голосом он говорит, — заодно это и
# проверка, что он жив: готовые фразы берутся из кэша без сети, и без такого
# вопроса робот говорил бы ими и при выключенном ПК.
ГОЛОС_ПЕРЕСПРОС = 60.0
# Сколько после последнего ответа ПК верим, что он жив, и берём готовые
# фразы из кэша. Дольше — сначала спросим сам ПК.
ЖИВ_СВЕЖО = 90.0


class RemoteVoice:
    """Голос, который синтезирует домашний ПК.

    Piper на роботе крутится на Cortex-A55: каждая фраза сначала считается, и
    только потом звучит. Голос при этом ровный, как у диктора вокзала, — точку
    от вопроса не отличить.

    На ПК стоит silero: ударения расставляет сам, различает омографы и
    поднимает интонацию на вопросе. Считает быстрее реального времени.

    Своего голоса у робота нет: робот работает только в паре с ПК (решение
    хозяина, октябрь 2026). ПК не озвучил — фраза не звучит, а к ПК робот
    ненадолго не стучится. Раньше здесь был запасной piper — женский голос
    посреди мужского, с объявлением, что ПК пропал.

    Кэш заводится не сразу, а после первого настоящего ответа, и назван по
    голосу, которым ПК ответил НА САМОМ ДЕЛЕ. Раньше он назывался по тому, что
    записано в настройках робота, — а голос выбирается на ПК ключом --voice, и
    робот об этом не знал. Стоило поменять голос на ПК, и частые заготовки
    («Да?», «Не расслышал.», «Остановился.») продолжали звучать прежним
    голосом из кэша, а всё остальное — новым. Со стороны это ровно то самое
    «иногда прорезается какой-то другой голос», причём случайными фразами.
    """

    RATE = 24000

    def __init__(self, base: str, speaker: str = "",
                 cache: "PhraseCache | None" = None) -> None:
        self.base = base.rstrip("/")
        self.speaker = speaker
        self.cache = cache
        self.rate = self.RATE
        self._down_until = 0.0
        self._осечек = 0
        # Когда ПК в последний раз ответил (синтезом или на /health).
        self._жив_в = float("-inf")
        # Каким голосом ПК отвечает на самом деле. Пусто — ещё не спрашивали.
        self.голос = ""
        self._голос_в = 0.0

    def alive(self) -> bool:
        return time.monotonic() >= self._down_until

    def пк_ответил(self) -> None:
        """ПК ответил на другое (распознал фразу) — паузу снимаем сразу."""
        if self._down_until > time.monotonic():
            log.info("ПК снова отвечает — голос с ПК возвращается")
        self._down_until = 0.0
        self._осечек = 0
        self._жив_в = time.monotonic()

    def _осечка(self, что: str, e: Exception) -> None:
        self._осечек += 1
        пауза = min(PC_DOWN, PC_DOWN_FIRST * 2 ** (self._осечек - 1))
        self._down_until = time.monotonic() + пауза
        log.warning("ПК не %s (%s) — молчу, к ПК не хожу %.0f с", что, e, пауза)

    def _принять(self, имя: str) -> None:
        """Запоминает голос ПК и перенастраивает под него кэш."""
        имя = (имя or "").strip() or self.speaker
        if not имя or имя == self.голос:
            return
        if self.голос:
            log.info("голос на ПК сменился: %s → %s — кэш фраз теперь другой",
                     self.голос, имя)
        self.голос = имя
        if self.cache is not None:
            self.cache.voice = f"пк-{имя}"

    def _переспросить(self) -> None:
        """Каким голосом ПК говорит сейчас. Раз в несколько минут, В ФОНЕ.

        Спрашивать приходится отдельно, а не только по заголовку ответа на
        синтез: частые заготовки берутся из кэша и до ПК не доходят вовсе. То
        есть ровно те фразы, которые звучали бы прежним голосом, и не дали бы
        повода заметить, что голос сменился.

        А вот ЖДАТЬ ответа нельзя, и раньше мы ждали. Вопрос стоял первым
        делом в raw(), то есть раньше проверки кэша, — и фраза, которую робот
        уже умел сказать мгновенно, раз в пять минут упиралась в дорогу до
        ПК и обратно. Хозяйственный опрос на пути у речи: от его ответа для
        ЭТОЙ фразы не зависит ничего, он нужен следующим.

        Поэтому отдельным потоком. Голос сменится на фразу позже — это
        незаметно, а миллисекунды на каждой фразе заметны.
        """
        if self._голос_в and time.monotonic() - self._голос_в < ГОЛОС_ПЕРЕСПРОС:
            return
        # Отметку ставим ДО запроса, а не после удачи. Иначе выключенный ПК
        # получал бы этот вопрос перед каждой фразой, и каждая платила бы
        # секундой ожидания за ответ, которого не будет.
        self._голос_в = time.monotonic()
        threading.Thread(target=self._спросить_голос, daemon=True).start()

    def _спросить_голос(self) -> None:
        try:
            with urllib.request.urlopen(self.base + "/health",
                                        timeout=PC_CONNECT) as resp:
                сведения = json.loads(resp.read() or b"{}") or {}
        except (urllib.error.HTTPError, ValueError) as e:
            # Ответил, но не то, — значит, ПК жив: голос просто не узнали.
            log.debug("ПК на /health ответил непонятно (%s)", e)
            self._жив_в = time.monotonic()
            return
        except (urllib.error.URLError, OSError) as e:
            # ПК не ответил даже на /health — значит, выключен: готовые
            # фразы из кэша больше не звучат (своего голоса нет, а кэш — это
            # голос ПК), пока ПК не ответит снова.
            self._осечка("ответил на /health", e)
            return
        self._жив_в = time.monotonic()
        self._принять(str(сведения.get("голос_чей") or ""))

    def raw(self, text: str) -> bytes | None:
        """Сырой звук фразы. None — ПК не смог, и фраза не прозвучит."""
        if not self.alive():
            return None
        self._переспросить()
        # До первого ответа ПК мы не знаем, каким голосом он говорит, и брать
        # из кэша нельзя: там может лежать прошлый голос. И давно молчащему
        # ПК не верим: готовая фраза из кэша при выключенном ПК — это робот,
        # говорящий без ПК.
        if (self.cache is not None and self.голос
                and time.monotonic() - self._жив_в < ЖИВ_СВЕЖО):
            got = self.cache.get(text)
            if got is not None:
                return got
        body = json.dumps({"text": text, "voice": self.speaker},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/tts", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=PC_READ) as resp:
                wav = resp.read()
                self._принять(resp.headers.get("X-Voice") or "")
            with wave.open(io.BytesIO(wav)) as w:
                self.rate = w.getframerate()
                pcm = w.readframes(w.getnframes())
        except (urllib.error.URLError, OSError, wave.Error, EOFError) as e:
            self._осечка("озвучил", e)
            return None
        self._осечек = 0
        self._жив_в = time.monotonic()
        log.debug("ПК озвучил за %.2f с: %r", time.monotonic() - started, text[:60])
        if self.cache is not None and self.голос:
            self.cache.put(text, pcm)
        return pcm


def aplay_cmd(sample_rate: int, device: str = "") -> list[str]:
    """Команда проигрывания сырого моно-PCM.

    Устройство собирается здесь, а не в двух местах по отдельности: свой синтез
    и синтез с ПК играют через один и тот же динамик, и разъехаться они не
    должны. Пусто — карта по умолчанию, как было до появления USB-звука.
    """
    cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
           "-r", str(sample_rate)]
    if device.strip():
        cmd += ["-D", device.strip()]
    return cmd + ["-"]


class ПервыйЗвук:
    """Одна засечка: когда из этой реплики впервые пошёл звук.

    Нужна измерителю задержки (app.Turn), и без неё самое дорогое звено не
    видно вовсе. Снаружи заметно только, когда текст ОТДАЛИ синтезу, — а от
    этого момента до первого сэмпла в динамике проходит синтез на ПК и
    дорога до колонки. Пока это не измерено, «робот тормозит» чинится наугад.

    Общий предок у двух видов реплики (свой динамик и браузер): оба обязаны
    отвечать на один и тот же вопрос одинаково, иначе цифры из журнала нельзя
    будет сравнивать между режимами.
    """

    def __init__(self) -> None:
        self.on_sound: Callable[[], None] | None = None
        self._прозвучал = False
        # Кому отдать звук перед динамиком — лицу, под рот. Общее для всех
        # трёх видов реплики: раньше провод был только у RemoteSpeech, а
        # робот по умолчанию говорит в браузер (ROBOT_AUDIO_OUT=browser) —
        # то есть рот по звуку не работал в самом частом режиме.
        self.on_pcm: Callable[[bytes, int], None] | None = None
        # Какое предложение ПОШЛО В ДИНАМИК прямо сейчас — лицу, под текст.
        # Не то, что модель дописала, а то, что звучит: модель опережает
        # синтез на фразу-две, и текст «говорю» на экране без этого бежал
        # впереди голоса.
        self.on_sentence: Callable[[str], None] | None = None

    def звук_пошёл(self) -> None:
        if self._прозвучал or self.on_sound is None:
            return
        self._прозвучал = True
        try:
            self.on_sound()
        except Exception:                       # noqa: BLE001
            # Измеритель не смеет ломать речь. Молчащий робот из-за неверной
            # засечки — цена, несоизмеримая с пользой от засечки.
            log.exception("не смог отметить первый звук")

    def под_рот(self, pcm: bytes, rate: int) -> None:
        """Отдать кусок звука лицу. Лицо не смеет ломать речь."""
        if self.on_pcm is None or not pcm:
            return
        try:
            self.on_pcm(pcm, int(rate))
        except Exception:                       # noqa: BLE001
            log.debug("рот по звуку не взял фразу", exc_info=True)

    def фраза_пошла(self, sentence: str) -> None:
        """Предложение уходит в динамик — лицу под текст «говорю»."""
        if self.on_sentence is None:
            return
        try:
            self.on_sentence(sentence)
        except Exception:                       # noqa: BLE001
            log.debug("лицо не взяло текст фразы", exc_info=True)


class RemoteSpeech(ПервыйЗвук):
    """Реплика в свой динамик, синтезированная на ПК.

    Готовые куски приходят по сети, и мы кладём их в aplay. Синтез идёт в
    своём потоке — иначе он встал бы поперёк чтения ответа модели.

    ПК замолчал посреди реплики — остаток не звучит: своего голоса у робота
    нет (раньше доканчивал свой piper, чужим голосом).
    """

    def __init__(self, remote: RemoteVoice, volume: float = 1.0,
                 device: str = "") -> None:
        super().__init__()
        self.remote = remote
        self.volume = volume
        # Сколько предложений реально ушло в динамик: ни одного — значит,
        # реплику никто не слышал, и таймер прозвонит ещё раз.
        self.прозвучало = 0
        # Частота, на которой ОТКРЫТ aplay, — снимок на момент запуска.
        # RemoteVoice.raw переписывает remote.rate по каждому ответу ПК, а
        # труба в aplay уже открыта и частоту сменить не может: всё, что в
        # неё идёт, приводится к этой.
        self._rate = int(remote.rate)
        self._aplay = subprocess.Popen(
            aplay_cmd(self._rate, device),
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._queue: queue.Queue = queue.Queue()
        self._cancelled = False
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def feed(self, sentence: str) -> None:
        sentence = speakable(sentence)
        if sentence and not self._cancelled:
            self._queue.put(sentence)

    def cancel(self) -> None:
        """«Замолчи»: бросить очередь и оборвать то, что уже в динамике.

        Одного флага мало, и раньше был только он: фразы, уже лежащие в
        буфере aplay, звучали до конца, а те, что стояли в очереди на
        синтез, — до флага не доходили лишь по счастливому порядку. Теперь
        очередь вычищается, а aplay убивается: он бросает буфер карты, и
        тишина наступает сразу, а не через две фразы.
        """
        self._cancelled = True
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._aplay.kill()
        except OSError:
            pass
        # Разбудить поток синтеза, если он ждёт очередь: пусть выйдет.
        self._queue.put(None)

    def _run(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:
                break
            if self._cancelled:
                continue
            pcm = self.remote.raw(sentence)
            if not pcm or self._cancelled:
                continue
            # В aplay — только на его частоте: он открыт один раз на реплику,
            # а ПК (сменили голос) может ответить на другой.
            pcm = пересчитать_частоту(pcm, int(self.remote.rate), self._rate)
            self.под_рот(pcm, self._rate)
            try:
                self.фраза_пошла(sentence)
                self._aplay.stdin.write(scale(pcm, self.volume))
                self._aplay.stdin.flush()
                self.прозвучало += 1
                self.звук_пошёл()
            except (BrokenPipeError, ValueError, OSError) as e:
                if not self._cancelled:
                    log.warning("звук до динамика не дошёл: %s", e)
                break
        try:
            self._aplay.stdin.close()
        except OSError:
            pass

    def close(self) -> int:
        self._queue.put(None)
        # После «замолчи» ждать поток незачем: он может висеть в запросе к
        # ПК, и всё, что он принесёт, всё равно выбрасывается. Секунда — и отпускаем; поток фоновый, доживёт сам.
        self._worker.join(timeout=1.0 if self._cancelled else 120)
        try:
            self._aplay.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("aplay завис — убиваю")
            self._aplay.kill()
            self._aplay.wait(timeout=5)
        return 1 if self.прозвучало else 0


class WebSpeech(ПервыйЗвук):
    """Реплика робота в браузер: каждое предложение уходит, как только готово.

    Синтез идёт в своём потоке — иначе он вставал бы поперёк чтения ответа
    модели, и выигрыша от потоковости не было бы вовсе.
    """

    def __init__(self, endpoint: str, volume: float = 1.0,
                 remote: "RemoteVoice | None" = None) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.volume = volume
        # Голос с ПК. Другого нет: не озвучил ПК — фраза не звучит.
        self.remote = remote

        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._said: list[str] = []
        self._listeners = 0
        self._cancelled = False
        # До какого момента звук будет играть в браузере. Клипы встают в
        # очередь вкладки друг за другом, поэтому срок наращиваем, а не
        # пересчитываем: иначе микрофон включится посреди второй фразы.
        self._until = 0.0

    # --- со стороны разговора --------------------------------------------
    def feed(self, sentence: str) -> None:
        sentence = speakable(sentence)
        if not sentence or self._cancelled:
            return
        self._said.append(sentence)
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()
        self._queue.put(sentence)

    def cancel(self) -> None:
        """«Замолчи»: не синтезировать то, что ещё не ушло."""
        self._cancelled = True

    def close(self) -> int:
        """Ждёт, пока всё сказанное действительно отзвучит.

        Возвращает, сколько вкладок реально проиграли реплику.
        """
        if self._worker is None:
            return 0
        self._queue.put(None)
        # Синтез длинной реплики небыстрый, но вечно ждать нельзя: всё это
        # время робот глухой.
        self._worker.join(timeout=120)

        if self._listeners and not self._cancelled:
            # Ждём конца звучания. Иначе микрофон включится раньше динамика, и
            # робот услышит сам себя: на живом роботе это дало «Водои и
            # триданцы, точка муфе» вместо «Батарея двенадцать вольт».
            # Аппаратного эхоподавления нет, полагаемся на длительность.
            left = self._until + BROWSER_LAG - time.monotonic()
            if left > 0:
                time.sleep(left)
        return self._listeners

    # --- со стороны синтеза ----------------------------------------------
    def _run(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:
                return
            if self._cancelled:
                continue
            готово = self._synthesize(sentence)
            if готово is None or self._cancelled:
                continue
            raw, rate = готово
            # Рот по звуку — и в браузерном режиме тоже: звук здесь в руках
            # целиком, а этот режим у робота по умолчанию.
            self.под_рот(raw, rate)
            ушло = time.monotonic()
            heard = self._post(_as_wav(raw, rate), sentence)
            log.info("реплика до пульта: %.0f мс, %d КБ",
                     (time.monotonic() - ушло) * 1000.0, len(raw) // 1024)
            # Звук родился и уехал играть. Слушает его кто-то или нет — вопрос
            # отдельный: если вкладок нет, задержка тут ни при чём.
            self.звук_пошёл()
            # Текст на экран — когда клип ЗАИГРАЕТ, а не когда уехал: он
            # встаёт в очередь вкладки за предыдущим, и без этого экран
            # показывал бы вторую фразу, пока звучит первая.
            через = max(0.0, self._until - time.monotonic()) if heard else 0.0
            self._объявить_фразу(sentence, через)
            if heard:
                self._listeners = max(self._listeners, heard)
                seconds = max(0.0, len(raw) / 2 / rate)
                # Клип встаёт в хвост очереди вкладки: если предыдущий ещё
                # играет, этот начнётся после него.
                self._until = max(self._until, time.monotonic()) + seconds

    def _объявить_фразу(self, sentence: str, через: float) -> None:
        if через < 0.05:
            self.фраза_пошла(sentence)
            return

        def позже() -> None:
            if not self._cancelled:
                self.фраза_пошла(sentence)

        t = threading.Timer(через, позже)
        t.daemon = True
        t.start()

    def _synthesize(self, text: str) -> tuple[bytes, int] | None:
        """Звук фразы с ПК и его частота. None — ПК не озвучил, фраза молчит.

        Засекается и пишется в журнал: ПК или кэш его голоса — разница в
        разы, и без журнала спорить, где теряется секунда, можно вечно.
        """
        if self.remote is None:
            return None
        начало = time.monotonic()
        pcm = self.remote.raw(text)
        if pcm is None:
            return None
        self._засечь("ПК", начало, text)
        return scale(pcm, self.volume), self.remote.rate

    @staticmethod
    def _засечь(откуда: str, начало: float, text: str) -> None:
        мс = (time.monotonic() - начало) * 1000.0
        log.info("синтез (%s): %.0f мс на %d знаков", откуда, мс, len(text))

    def _post(self, wav: bytes, text: str) -> int:
        """Отдаёт реплику пульту. Возвращает число слушающих вкладок."""
        req = urllib.request.Request(
            self.endpoint, data=wav, method="POST",
            headers={
                "Content-Type": "audio/wav",
                # Заголовки — только ASCII, поэтому текст экранируем.
                "X-Robot-Text": urllib.parse.quote(text),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("не смог отдать реплику пульту: %s", e)
            return 0

        listeners = int(info.get("listeners") or 0)
        if not listeners:
            # Вкладок может быть сколько угодно, а звук — выключен в каждой:
            # различать эти два случая важно, иначе «почему таймер прозвонил
            # в пустоту» разбирается гаданием.
            tabs = int(info.get("tabs") or 0)
            log.info("реплику никто не услышит: вкладок %d, из них со звуком 0", tabs)
        return listeners


class Speaker:
    """Голос робота — это голос ПК, и только он.

    Робот работает только в паре с ПК (решение хозяина, октябрь 2026). Нет
    адреса ПК или ПК не отвечает — робот молчит: ни своего piper, ни
    объявлений о том, что ПК пропал. Будильник и таймер в такую минуту не
    теряются — say вернёт «никто не слышал», и они прозвонят снова.
    """

    def __init__(self, *, audio_out: str = "local",
                 web_endpoint: str = "http://127.0.0.1:8000/speak",
                 volume: float = 1.0, cache_dir: Path | None = None,
                 pc_url: str = "", pc_voice: str = "",
                 spk_device: str = "") -> None:
        self.audio_out = audio_out
        self.web_endpoint = web_endpoint
        self.spk_device = spk_device
        # Голос с ПК; кэш — его готовых фраз («Да?», «Остановился.»).
        self.remote = None
        if pc_url:
            self.remote = RemoteVoice(
                pc_url, pc_voice,
                PhraseCache(cache_dir, f"пк-{pc_voice or 'голос'}")
                if cache_dir is not None else None)
        else:
            log.error("адреса ПК нет — робот молчит: своего голоса у него нет")
        # Что произносится прямо сейчас — чтобы «замолчи» успело перехватить
        # то, что ещё не ушло в пульт или не отзвучало в динамике. Во ВСЕХ
        # режимах: раньше запоминалась только браузерная реплика, и на
        # своём динамике «замолчи» не обрывало ничего.
        self._current: ПервыйЗвук | None = None
        self.enabled = self.remote is not None
        # Громкость 0.1–1.0. Меняется голосом и на ночь.
        self.volume = volume
        # Ночью тише: спрашиваем у настроек, наступили ли тихие часы.
        self.quiet_now: Callable[[], bool] | None = None
        self.quiet_volume = 0.45
        # Кого известить, когда громкость поменяли: настройку надо сохранить,
        # иначе автообновление ночью вернёт её обратно.
        self.on_volume: Callable[[float], None] | None = None
        # Последняя сказанная фраза — для «повтори».
        self.last_said = ""
        # Сколько раз реплику обрывали на полуслове. Растёт в hush.
        self.hushes = 0
        # Кому отдавать звук под рот на экране (лицо).
        self.on_pcm: Callable[[bytes, int], None] | None = None


    def может_говорить(self) -> bool:
        """Есть ли сейчас чем говорить: ПК задан и не отворачивались от него.

        Голосу робота (Voice.say) это нужно ДО речи: незачем глушить микрофон
        и писать на лицо «говорю», если звука не будет.
        """
        return self.remote is not None and self.remote.alive()

    def effective_volume(self) -> float:
        """Насколько громко говорить прямо сейчас."""
        if self.quiet_now is not None and self.quiet_now():
            return min(self.volume, self.quiet_volume)
        return self.volume

    def set_volume(self, level: float) -> None:
        self.volume = max(0.1, min(1.0, float(level)))
        log.info("громкость %.0f%%", self.volume * 100)
        if self.on_volume is not None:
            self.on_volume(self.volume)

    def hush(self) -> None:
        """Прекратить то, что уже произносится.

        В режиме browser реплики стоят в очереди вкладки: робот своё уже
        отправил и молчать сам по себе не начнёт. Поэтому просим пульт
        бросить очередь.

        На своём динамике (local) реплика — это aplay со звуком ПК, и
        обрывает её cancel() у текущей реплики. Раньше здесь стоял выход по режиму
        ДО cancel, и «Кузя, стоп» на своём динамике не значило ничего:
        счётчик рос, а робот договаривал рассказ до конца.
        """
        self.last_said = ""
        # Считаем прерывания, а не просто чистим поле: тот, кто вёл ход,
        # допишет last_said в своём finally уже ПОСЛЕ нас и затрёт очистку.
        # По разнице счётчика он поймёт, что реплику оборвали, и не станет
        # выдавать за сказанное то, чего человек не дослушал.
        self.hushes += 1
        # Сначала своё: реплика синтезируется по предложениям, и часть их ещё
        # не ушла. Без этого пульт бросит очередь, а мы тут же дошлём хвост.
        current = self._current
        if current is not None:
            try:
                current.cancel()
            except Exception:                   # noqa: BLE001
                log.exception("не смог оборвать реплику")
        if self.audio_out != "browser":
            return
        req = urllib.request.Request(self.web_endpoint + "/stop", data=b"",
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except (urllib.error.URLError, OSError) as e:
            log.warning("не смог остановить речь в пульте: %s", e)

    def stream(self, *, loud: bool = False):
        """Начать реплику. Куда она пойдёт — решает audio_out.

        loud — не приглушать: будильник и таймер человек ставил сам, и они
        должны звучать в полную громкость даже в тихие часы.
        """
        if not self.может_говорить():
            return None
        volume = 1.0 if loud else self.effective_volume()
        if self.audio_out == "browser":
            speech = WebSpeech(self.web_endpoint, volume, remote=self.remote)
        else:
            try:
                speech = RemoteSpeech(self.remote, volume, self.spk_device)
            except OSError:
                log.exception("не смог запустить aplay")
                return None
        # Рот по звуку — звук ПК проходит через Python в обоих режимах.
        speech.on_pcm = self.on_pcm
        self._current = speech
        return speech

    def say(self, text: str, *, loud: bool = False, on_sound=None) -> int:
        """Синхронно проговорить готовый текст.

        Возвращает, сколько слушателей реально услышали: таймеру это нужно,
        чтобы не прозвонить в закрытую вкладку и не забыть об этом.

        on_sound — засечка первого звука для секундомера задержки. Ставится
        именно здесь, а не после возврата: say ЖДЁТ, пока реплика отзвучит, и
        отметка после него меряет длину речи, а не задержку до неё.
        """
        log.info("робот: %s", text)
        было_прерываний = self.hushes
        speech = self.stream(loud=loud)
        if speech is not None and on_sound is not None:
            speech.on_sound = on_sound
        if speech is None:
            # Говорить нечем — ПК нет. Честно: никто не слышал. Таймер по
            # этому прозвонит ещё раз, когда ПК вернётся, а не забудет.
            return 0
        for sentence in split_sentences(text):
            speech.feed(sentence)
        слышали = speech.close()
        # Запоминаем как сказанное только то, что прозвучало целиком. Раньше
        # поле заполнялось ДО синтеза: не было звука — «повтори» повторяло
        # фразу, которой человек не слышал, а оборванная «замолчи» репликой
        # возвращалась из небытия. Не прозвучало ничего — тоже не сказано.
        if self.hushes == было_прерываний and слышали > 0:
            self.last_said = text
        return слышали


def _as_wav(raw: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


def split_sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENTENCE_END.split(text)) if s]


# Конец предложения в потоке модели: знак, за ним, может быть, кавычка или
# скобка, и обязательно пробел — «3.5» и «т.е.» без пробела концом не считаем.
_КОНЕЦ = re.compile(r"[.!?…]+[»\"”)]*\s+")

# После каких сокращений точка — не конец предложения. Модель пишет «т. е.»,
# «и т. д.», «и др.», и раньше синтез получал «т.» отдельной фразой, а её же
# считали в предел фраз — вторая настоящая фраза ответа выбрасывалась.
_СОКРАЩЕНИЯ = frozenset({"т", "е", "д", "п", "к", "др", "пр", "см", "напр",
                         "тыс", "млн", "млрд", "руб", "коп", "г", "гг", "ул",
                         "св", "ст"})

# Где можно отдать в синтез первую клаузу: запятая или двоеточие, тире в
# пробелах. «3,5» и «кто-то» сюда не попадают — за знаком нет пробела.
_КЛАУЗА = re.compile(r"(?:[,:;]|\s[—–-])\s+")

# Сколько слов должно набраться до первой клаузы. Короче — «Хорошо,» или
# «Ой,» отдельным куском: синтез потратит на него свой круг до ПК, а звучать
# будет обрывком.
КЛАУЗА_СЛОВ = 4


def _конец_предложения(текст: str) -> tuple[int, int] | None:
    """(где кончилось предложение, где начинается следующее) или None."""
    for m in _КОНЕЦ.finditer(текст):
        if текст[m.start()] == ".":
            слово = re.search(r"(\w+)$", текст[:m.start()])
            if слово and слово.group(1).lower() in _СОКРАЩЕНИЯ:
                continue
        return m.end() - len(m.group()) + len(m.group().rstrip()), m.end()
    return None


def _клауза(текст: str) -> int | None:
    """Где можно отрезать первую клаузу (позиция после знака) или None."""
    for m in _КЛАУЗА.finditer(текст):
        до = текст[:m.start()]
        # «3, 5 и 7» — перечисление чисел, резать его на голос незачем.
        if до[-1:].isdigit() and текст[m.end():m.end() + 1].isdigit():
            continue
        if len(до.split()) >= КЛАУЗА_СЛОВ:
            return m.end() - len(m.group()) + len(m.group().rstrip())
    return None


class SentenceBuffer:
    """Копит поток токенов и отдаёт законченные куски речи.

    Первый кусок ответа — не предложение, а клауза: до первой запятой,
    двоеточия или тире, если до них набралось КЛАУЗА_СЛОВ слов. Так делают
    голосовые помощники (Unmute, Thinker–Talker у Qwen): синтез начинает,
    пока модель дописывает первое предложение, и первый звук приходит на
    десятые доли секунды раньше — на длинной первой фразе это заметно.

    Остаток первого предложения отдаётся ВМЕСТЕ со следующим. Это не
    красота, а счёт: главный цикл считает куски как фразы (предел фраз), и
    отдельный остаток съел бы место второго предложения — вторая фраза
    ответа молча выбрасывалась бы. С приклеиванием кусков не больше, чем
    предложений, а у ответа из одного предложения — два куска.
    """

    def __init__(self) -> None:
        self._buf = ""
        # Первую клаузу ещё можно отрезать (только в первом предложении).
        self._первое = True
        # Остаток первого предложения после клаузы: None — его нет; строка —
        # дописан и ждёт, чтобы уйти вместе со следующим предложением.
        self._хвост: str | None = None
        # Клауза отдана, а первое предложение ещё не кончилось.
        self._внутри_первого = False

    def push(self, chunk: str) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        while True:
            конец = _конец_предложения(self._buf)
            if конец is None:
                break
            до, после = конец
            фраза = self._buf[:до].strip()
            self._buf = self._buf[после:]
            if not фраза:
                continue
            if self._внутри_первого:
                # Первое предложение дописано после клаузы — ждём второе.
                self._внутри_первого = False
                self._хвост = фраза
                continue
            self._первое = False
            if self._хвост is not None:
                фраза, self._хвост = f"{self._хвост} {фраза}", None
            out.append(фраза)
        # Буфер уже кончается точкой — первое предложение, скорее всего,
        # дописано целиком, и резать его на клаузу поздно: выигрыша нет, а
        # шов в интонации есть.
        if (self._первое and not self._внутри_первого
                and not re.search(r"[.!?…][»\"”)]*$", self._buf.rstrip())):
            где = _клауза(self._buf)
            if где is not None:
                клауза = self._buf[:где].strip()
                self._buf = self._buf[где:].lstrip()
                self._первое = False
                self._внутри_первого = True
                out.append(клауза)
        return out

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        if self._хвост is not None:
            rest, self._хвост = f"{self._хвост} {rest}".strip(), None
        self._внутри_первого = False
        return rest
