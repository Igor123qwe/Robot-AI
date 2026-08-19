"""Синтез речи — Piper. Куда его выводить, зависит от того, что подключено.

  local   — сразу в aplay. Piper запускается один раз на реплику, читает stdin
            построчно и отдаёт сырой звук, поэтому робот начинает говорить, не
            дожидаясь, пока Claude допишет ответ. Так будет, когда приедет
            динамик SOTAMIA.
  browser — реплика собирается в WAV и уходит в веб-сервер робота, а играет её
            браузер с открытым пультом. Временно, пока своего динамика нет:
            IP Webcam умеет отдавать звук, но не принимать.

В режиме browser реплика синтезируется по предложениям и уходит в пульт по
мере готовности. Раньше она копилась целиком и синтез начинался только после
последнего слова модели — то есть вся потоковая машинерия не давала ничего, а
две-три секунды синтеза честно ждали своей очереди за десятью секундами
генерации. Пульт умеет очередь клипов, сервер хранит последние двенадцать.

Плата за это — Piper поднимается чаще. Поэтому здесь же кэш готовых фраз:
правила отвечают одними и теми же словами («Не расслышал.», «Остановился.»,
«Да?»), и синтезировать их заново каждый раз — это полторы секунды на пустом
месте, причём в самых частых ответах.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import queue
import re
import shutil
import subprocess
import sys
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
    """Готовит текст к произнесению: убирает то, что голосом не передать."""
    return re.sub(r"\s+", " ", _PICTURES.sub(" ", text)).strip()


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
PC_READ = 20.0
# Насколько отворачиваемся от ПК, когда он не озвучил. Раньше здесь было
# глухих шестьдесят секунд на любую осечку — и одна сетевая икота посреди
# разговора переводила робота на свой piper на минуту вперёд. Голоса разные:
# на ПК мужской silero, свой piper женский, — и человек слышал, как посреди
# разговора «прорезается другой голос», не понимая почему.
#
# Поэтому лесенкой: первая осечка стоит пять секунд, дальше вдвое. Выключенный
# ПК за три-четыре фразы всё равно доберётся до минуты, а короткий сбой связи
# обойдётся одной фразой чужим голосом вместо двадцати.
PC_DOWN_FIRST = 5.0
PC_DOWN = 60.0

# Как часто переспрашивать ПК, каким голосом он говорит. Меняется это редко —
# только когда человек перезапускает сервер с другим --voice, — а стоит вопрос
# один короткий запрос по домашней сети.
ГОЛОС_ПЕРЕСПРОС = 300.0


class RemoteVoice:
    """Голос, который синтезирует домашний ПК.

    Piper на роботе крутится на Cortex-A55: каждая фраза сначала считается, и
    только потом звучит. Голос при этом ровный, как у диктора вокзала, — точку
    от вопроса не отличить.

    На ПК стоит silero: ударения расставляет сам, различает омографы и
    поднимает интонацию на вопросе. Считает быстрее реального времени.

    Отступление обязательно. ПК выключают, и робот от этого не должен неметь:
    не ответил — переходим на свой piper и ненадолго туда не ходим. Ровно так
    же устроено распознавание, и по той же причине.

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
        # Каким голосом ПК отвечает на самом деле. Пусто — ещё не спрашивали.
        self.голос = ""
        self._голос_в = 0.0

    def alive(self) -> bool:
        return time.monotonic() >= self._down_until

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
        """Каким голосом ПК говорит сейчас. Раз в несколько минут.

        Спрашивать приходится отдельно, а не только по заголовку ответа на
        синтез: частые заготовки берутся из кэша и до ПК не доходят вовсе. То
        есть ровно те фразы, которые звучали бы прежним голосом, и не дали бы
        повода заметить, что голос сменился.
        """
        if self._голос_в and time.monotonic() - self._голос_в < ГОЛОС_ПЕРЕСПРОС:
            return
        # Отметку ставим ДО запроса, а не после удачи. Иначе выключенный ПК
        # получал бы этот вопрос перед каждой фразой, и каждая платила бы
        # секундой ожидания за ответ, которого не будет.
        self._голос_в = time.monotonic()
        try:
            with urllib.request.urlopen(self.base + "/health",
                                        timeout=PC_CONNECT) as resp:
                сведения = json.loads(resp.read() or b"{}") or {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Не смогли — не беда: голос выяснится из заголовка первого же
            # настоящего синтеза. Отворачиваться от ПК из-за этого нельзя.
            log.debug("не спросил у ПК про голос: %s", e)
            return
        self._принять(str(сведения.get("голос_чей") or ""))

    def raw(self, text: str) -> bytes | None:
        """Сырой звук фразы. None — ПК не смог, говорим сами."""
        if not self.alive():
            return None
        self._переспросить()
        # До первого ответа ПК мы не знаем, каким голосом он говорит, и брать
        # из кэша нельзя: там может лежать прошлый голос.
        if self.cache is not None and self.голос:
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
            self._осечек += 1
            пауза = min(PC_DOWN, PC_DOWN_FIRST * 2 ** (self._осечек - 1))
            self._down_until = time.monotonic() + пауза
            log.warning("ПК не озвучил (%s) — говорю своим голосом %.0f с", e, пауза)
            return None
        self._осечек = 0
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
    этого момента до первого сэмпла в динамике проходит холодный старт piper,
    сам синтез и дорога до колонки. Пока это не измерено, «робот тормозит»
    чинится наугад.

    Общий предок у трёх видов реплики (свой piper, синтез на ПК, браузер): все
    три обязаны отвечать на один и тот же вопрос одинаково, иначе цифры из
    журнала нельзя будет сравнивать между режимами.
    """

    def __init__(self) -> None:
        self.on_sound: Callable[[], None] | None = None
        self._прозвучал = False

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


class Speech(ПервыйЗвук):
    """Одна реплика робота в динамик. Предложения докидываются по мере генерации."""

    def __init__(self, piper_cmd: list[str], sample_rate: int,
                 volume: float = 1.0, device: str = "") -> None:
        super().__init__()
        self._piper = subprocess.Popen(
            piper_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        aplay = aplay_cmd(sample_rate, device)
        self._pump: threading.Thread | None = None
        if volume >= 0.999:
            # Обычный случай: piper пишет прямо в aplay, Python не при делах.
            self._aplay = subprocess.Popen(
                aplay, stdin=self._piper.stdout, stderr=subprocess.DEVNULL)
            # Дескриптор нужен только aplay — иначе он не увидит EOF.
            self._piper.stdout.close()
        else:
            # Тише — значит звук надо потрогать по дороге.
            self._aplay = subprocess.Popen(
                aplay, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._pump = threading.Thread(
                target=self._transfer, args=(volume,), daemon=True)
            self._pump.start()

    def _transfer(self, volume: float) -> None:
        broken = False
        try:
            while True:
                chunk = self._piper.stdout.read(4096)
                if not chunk:
                    break
                self.звук_пошёл()
                self._aplay.stdin.write(scale(chunk, volume))
        except (BrokenPipeError, ValueError, OSError) as e:
            broken = True
            log.warning("звук до динамика не дошёл: %s", e)
        finally:
            # Закрыть надо в любом случае: иначе aplay ждёт EOF, а piper —
            # свободного места в трубе, и close() честно висит две минуты.
            for pipe in (self._aplay.stdin, self._piper.stdout if broken else None):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass

    def feed(self, sentence: str) -> None:
        sentence = speakable(sentence)
        if not sentence or self._piper.stdin is None:
            return
        try:
            self._piper.stdin.write((sentence + "\n").encode())
            self._piper.stdin.flush()
        except BrokenPipeError:
            log.warning("piper: процесс закрылся раньше времени")
        if self._pump is None:
            # На полной громкости piper пишет прямо в aplay, минуя Python, и
            # первый сэмпл нам не виден. Отмечаем момент отдачи текста и
            # называем вещи своими именами: настоящий звук будет чуть позже.
            # Гонять звук через Python ради засечки нельзя — это его главный
            # тракт, и лишний прыжок в нём дороже любой цифры в журнале.
            self.звук_пошёл()

    def close(self) -> int:
        """Дожидается, пока всё сказанное действительно прозвучит.

        Возвращает 1: динамик у робота свой, слушателя искать не надо.
        """
        try:
            if self._piper.stdin is not None:
                self._piper.stdin.close()
        except BrokenPipeError:
            pass
        for name, proc in (("piper", self._piper), ("aplay", self._aplay)):
            try:
                # Тридцати секунд хватает самой длинной реплике. Больше ждать
                # нельзя: всё это время робот глухой и немой.
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # Зависший процесс держит звуковую карту, и следующая реплика
                # её уже не откроет. Лучше убить, чем онеметь навсегда.
                log.warning("%s завис — убиваю", name)
                proc.kill()
                proc.wait(timeout=5)
        if self._pump is not None:
            self._pump.join(timeout=5)
        return 1


class RemoteSpeech(ПервыйЗвук):
    """Реплика в свой динамик, но синтезированная на ПК.

    Отличие от Speech только в источнике звука: там piper пишет в трубу сам,
    здесь готовые куски приходят по сети и мы кладём их в aplay. Синтез идёт в
    своём потоке — иначе он встал бы поперёк чтения ответа модели.

    Если ПК замолчал посреди реплики, доканчиваем своим piper: человек услышит
    смену голоса, но это несравнимо лучше оборванной на полуслове фразы.
    """

    def __init__(self, remote: RemoteVoice, fallback_cmd: list[str],
                 volume: float = 1.0, device: str = "") -> None:
        super().__init__()
        self.remote = remote
        self.fallback_cmd = fallback_cmd
        self.volume = volume
        self._aplay = subprocess.Popen(
            aplay_cmd(remote.rate, device),
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
        self._cancelled = True

    def _run(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:
                break
            if self._cancelled:
                continue
            pcm = self.remote.raw(sentence)
            if pcm is None:
                pcm = self._own(sentence)
            if not pcm or self._cancelled:
                continue
            try:
                self._aplay.stdin.write(scale(pcm, self.volume))
                self._aplay.stdin.flush()
                self.звук_пошёл()
            except (BrokenPipeError, ValueError, OSError) as e:
                log.warning("звук до динамика не дошёл: %s", e)
                break
        try:
            self._aplay.stdin.close()
        except OSError:
            pass

    def _own(self, sentence: str) -> bytes:
        """Запасной синтез своим piper, когда ПК недоступен."""
        try:
            return subprocess.run(
                self.fallback_cmd, input=sentence.encode(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=120, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("и свой piper не смог (%s)", e)
            return b""

    def close(self) -> int:
        self._queue.put(None)
        self._worker.join(timeout=120)
        try:
            self._aplay.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("aplay завис — убиваю")
            self._aplay.kill()
            self._aplay.wait(timeout=5)
        return 1


class WebSpeech(ПервыйЗвук):
    """Реплика робота в браузер: каждое предложение уходит, как только готово.

    Синтез идёт в своём потоке — иначе он вставал бы поперёк чтения ответа
    модели, и выигрыша от потоковости не было бы вовсе.
    """

    def __init__(self, piper_cmd: list[str], sample_rate: int, endpoint: str,
                 volume: float = 1.0, cache: "PhraseCache | None" = None,
                 remote: "RemoteVoice | None" = None) -> None:
        super().__init__()
        self.piper_cmd = piper_cmd
        self.sample_rate = sample_rate
        self.endpoint = endpoint
        self.volume = volume
        self.cache = cache
        # Голос с ПК, если он там поднят. Свой piper остаётся запасным.
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
            try:
                raw, rate = self._synthesize(sentence)
            except (OSError, subprocess.SubprocessError):
                log.exception("piper не смог синтезировать %r", sentence)
                continue
            if self._cancelled:
                continue
            ушло = time.monotonic()
            heard = self._post(_as_wav(raw, rate), sentence)
            log.info("реплика до пульта: %.0f мс, %d КБ",
                     (time.monotonic() - ушло) * 1000.0, len(raw) // 1024)
            # Звук родился и уехал играть. Слушает его кто-то или нет — вопрос
            # отдельный: если вкладок нет, задержка тут ни при чём.
            self.звук_пошёл()
            if heard:
                self._listeners = max(self._listeners, heard)
                seconds = max(0.0, len(raw) / 2 / rate)
                # Клип встаёт в хвост очереди вкладки: если предыдущий ещё
                # играет, этот начнётся после него.
                self._until = max(self._until, time.monotonic()) + seconds

    def _synthesize(self, text: str) -> tuple[bytes, int]:
        """Звук фразы и его частота. Сначала ПК, потом свой piper.

        Каждый путь засекается и пишется в журнал. Три источника звука стоят
        разного, и разница между ними — не проценты, а разы: кэш отдаёт готовое
        мгновенно, ПК считает быстрее реального времени, а свой piper на A55
        поднимается заново НА КАЖДОЕ ПРЕДЛОЖЕНИЕ и каждый раз грузит голосовую
        модель с нуля. Пока это не в журнале, спорить о том, где теряется
        секунда, можно бесконечно.
        """
        начало = time.monotonic()
        if self.remote is not None:
            pcm = self.remote.raw(text)
            if pcm is not None:
                self._засечь("ПК", начало, text)
                return scale(pcm, self.volume), self.remote.rate
        raw = None
        if self.cache is not None:
            raw = self.cache.get(text)
        if raw is not None:
            self._засечь("кэш", начало, text)
        if raw is None:
            raw = subprocess.run(
                self.piper_cmd, input=text.encode(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=120, check=True,
            ).stdout
            self._засечь("свой piper", начало, text)
            if self.cache is not None:
                # В кэш кладём звук полной громкости: тише сделаем на выходе,
                # иначе ночная громкость завела бы вторую копию каждой фразы.
                self.cache.put(text, raw)
        return scale(raw, self.volume), self.sample_rate

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
    def __init__(self, model_path: Path, *, audio_out: str = "local",
                 web_endpoint: str = "http://127.0.0.1:8000/speak",
                 volume: float = 1.0, cache_dir: Path | None = None,
                 pc_url: str = "", pc_voice: str = "",
                 spk_device: str = "") -> None:
        self.model_path = model_path
        self.audio_out = audio_out
        self.web_endpoint = web_endpoint
        self.spk_device = spk_device
        self.cache = (PhraseCache(cache_dir, model_path.name)
                      if cache_dir is not None else None)
        # Голос с ПК. Кэш у него свой: частота и тембр другие, и путать их
        # звуком от piper нельзя — робот заговорил бы чужим голосом на удвоенной
        # скорости.
        self.remote = None
        if pc_url:
            self.remote = RemoteVoice(
                pc_url, pc_voice,
                PhraseCache(cache_dir, f"пк-{pc_voice or 'голос'}")
                if cache_dir is not None else None)
        # Что произносится прямо сейчас — чтобы «замолчи» успело перехватить
        # то, что ещё не ушло в пульт.
        self._current: WebSpeech | None = None
        self.enabled = True
        self.sample_rate = 22050
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

        self.piper = _find_piper()
        if self.piper is None:
            log.error("piper не установлен — робот будет молчать (текст в логе)")
            self.enabled = False
            return
        if not model_path.exists():
            log.error("нет голоса %s — робот будет молчать", model_path)
            self.enabled = False
            return

        cfg = model_path.with_suffix(".onnx.json")
        if cfg.exists():
            try:
                self.sample_rate = json.loads(cfg.read_text())["audio"]["sample_rate"]
            except (ValueError, KeyError):
                log.warning("не разобрал %s, беру 22050 Гц", cfg.name)

    def _cmd(self) -> list[str]:
        return [self.piper, "--model", str(self.model_path), "--output-raw"]

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
        """
        self.last_said = ""
        # Считаем прерывания, а не просто чистим поле: тот, кто вёл ход,
        # допишет last_said в своём finally уже ПОСЛЕ нас и затрёт очистку.
        # По разнице счётчика он поймёт, что реплику оборвали, и не станет
        # выдавать за сказанное то, чего человек не дослушал.
        self.hushes += 1
        if self.audio_out != "browser":
            return
        # Сначала своё: реплика синтезируется по предложениям, и часть их ещё
        # не ушла. Без этого пульт бросит очередь, а мы тут же дошлём хвост.
        current = self._current
        if current is not None:
            current.cancel()
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
        if not self.enabled:
            return None
        volume = 1.0 if loud else self.effective_volume()
        if self.audio_out == "browser":
            speech = WebSpeech(self._cmd(), self.sample_rate, self.web_endpoint,
                               volume, cache=self.cache, remote=self.remote)
            self._current = speech
            return speech
        try:
            if self.remote is not None and self.remote.alive():
                return RemoteSpeech(self.remote, self._cmd(), volume,
                                    self.spk_device)
            return Speech(self._cmd(), self.sample_rate, volume,
                          self.spk_device)
        except OSError:
            log.exception("не смог запустить piper/aplay")
            return None

    def say(self, text: str, *, loud: bool = False) -> int:
        """Синхронно проговорить готовый текст.

        Возвращает, сколько слушателей реально услышали: таймеру это нужно,
        чтобы не прозвонить в закрытую вкладку и не забыть об этом.
        """
        log.info("робот: %s", text)
        было_прерываний = self.hushes
        speech = self.stream(loud=loud)
        if speech is None:
            # Синтеза нет вовсе (не встал piper): повторять нечего и незачем,
            # поэтому не притворяемся, что реплику потеряли по дороге.
            return 1
        for sentence in split_sentences(text):
            speech.feed(sentence)
        слышали = speech.close()
        # Запоминаем как сказанное только то, что прозвучало целиком. Раньше
        # поле заполнялось ДО синтеза: не встал piper — «повтори» повторяло
        # фразу, которой человек не слышал, а оборванная «замолчи» репликой
        # возвращалась из небытия.
        if self.hushes == было_прерываний:
            self.last_said = text
        return слышали


def _find_piper() -> str | None:
    """Ищет piper сначала в текущем venv, потом в PATH.

    Сервис запускается как .venv/bin/python, а venv/bin в PATH при этом нет —
    поэтому одного shutil.which недостаточно.
    """
    local = Path(sys.executable).with_name("piper")
    if local.exists():
        return str(local)
    return shutil.which("piper")


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


class SentenceBuffer:
    """Копит поток токенов и отдаёт законченные предложения."""

    def __init__(self) -> None:
        self._buf = ""

    def push(self, chunk: str) -> list[str]:
        self._buf += chunk
        parts = _SENTENCE_END.split(self._buf)
        if len(parts) == 1:
            return []
        self._buf = parts[-1]
        return [p.strip() for p in parts[:-1] if p.strip()]

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest
