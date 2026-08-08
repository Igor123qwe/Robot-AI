"""Захват звука и нарезка на фразы по детектору речи.

Три источника:
  local   — микрофон, воткнутый в RDK X5 (после приезда ReSpeaker Lite);
  phone   — старый Android с приложением IP Webcam, отдаёт /audio.wav;
  browser — микрофон компьютера: вкладка пульта шлёт звук на веб-сервер
            робота, а мы забираем его оттуда потоком. Удобно для отладки:
            наушники на голове, эха нет, телефон не нужен.

Режим строго половинного дуплекса: пока робот говорит, микрофон заглушён. Программный
AEC поверх Wi-Fi с плавающей задержкой всё равно не работает, так что barge-in
появится только вместе с аппаратным эхоподавлением ReSpeaker.
"""

from __future__ import annotations

import io
import logging
import struct
import subprocess
import threading
import time
import wave
from collections import deque
from typing import Callable, Iterator

import numpy as np
import webrtcvad

log = logging.getLogger(__name__)

FRAME_MS = 20  # webrtcvad принимает только 10, 20 или 30 мс

# Сколько «микрофон считается живым» после последнего настоящего звука.
# Пять секунд: человек молчит между фразами дольше, чем можно подумать,
# а вот закрытая вкладка молчит навсегда.
REAL_AUDIO_TTL = 5.0


# --------------------------------------------------------------------------
# Источники
# --------------------------------------------------------------------------
class LocalSource:
    """Микрофон, воткнутый в самого робота, — через arecord.

    Сначала здесь был sounddevice, и на ReSpeaker Lite он оказался бесполезен:
    PortAudio показывает эту карту как «0 in, 2 out», то есть микрофона на ней
    как будто нет вовсе. Он опрашивает устройство на своей частоте, карта
    отказывает — и вход просто исчезает из списка. При этом
    `arecord -D plughw:0,0` с той же картой пишет прекрасно.

    Поэтому запись идёт тем же способом, что и воспроизведение: отдельным
    процессом ALSA. Заодно это снимает вопрос частоты и формата — они заданы
    явно, а `plughw` приведёт их сам, если карта хочет иначе. И симметрия:
    микрофон и динамик настраиваются одинаковыми именами, обе стороны можно
    проверить руками через arecord/aplay, не поднимая робота.
    """

    def __init__(self, sample_rate: int, device: str = "") -> None:
        self.sample_rate = sample_rate
        self.frame_len = sample_rate * FRAME_MS // 1000
        self.device = device.strip() or "default"

    def frames(self) -> Iterator[np.ndarray]:
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
               "-r", str(self.sample_rate), "-D", self.device]
        log.info("аудио: слушаю %s", self.device)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        try:
            while True:
                buf = proc.stdout.read(self.frame_len * 2)
                if len(buf) < self.frame_len * 2:
                    # Ругань arecord тащим в текст ошибки: без неё «источник
                    # пропал» не отличить от «нет такой карты», и человек
                    # ищет обрыв связи там, где опечатка в имени устройства.
                    беда = (proc.stderr.read() or b"").decode(
                        "utf-8", "replace").strip()
                    raise ConnectionError(
                        f"arecord замолчал ({self.device})"
                        + (f": {беда.splitlines()[-1]}" if беда else ""))
                yield np.frombuffer(buf, dtype="<i2")
        finally:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("arecord не закрылся")


class PhoneSource:
    """Микрофон телефона: HTTP-поток WAV от IP Webcam."""

    def __init__(self, base_url: str, sample_rate: int) -> None:
        self.url = base_url.rstrip("/") + "/audio.wav"
        self.sample_rate = sample_rate
        self.frame_len = sample_rate * FRAME_MS // 1000

    def frames(self) -> Iterator[np.ndarray]:
        import requests

        log.info("аудио: подключаюсь к %s", self.url)
        resp = requests.get(self.url, stream=True, timeout=(5, 30))
        resp.raise_for_status()

        raw = resp.raw
        src_rate, channels = _read_wav_header(raw)
        log.info("аудио: поток %d Гц, каналов %d", src_rate, channels)

        # Читаем блоками, приводим к 16 кГц моно, режем на кадры по 20 мс.
        tail = np.empty(0, dtype=np.int16)
        chunk_bytes = src_rate * channels * 2 // 5  # ~200 мс
        while True:
            buf = raw.read(chunk_bytes)
            if not buf:
                raise ConnectionError("аудиопоток телефона оборвался")
            samples = np.frombuffer(buf[: len(buf) // 2 * 2], dtype="<i2")
            if channels > 1:
                samples = samples.reshape(-1, channels)[:, 0]
            if src_rate != self.sample_rate:
                samples = _resample(samples, src_rate, self.sample_rate)

            tail = np.concatenate([tail, samples])
            n = len(tail) // self.frame_len
            for i in range(n):
                yield tail[i * self.frame_len:(i + 1) * self.frame_len]
            tail = tail[n * self.frame_len:]


def _read_wav_header(stream) -> tuple[int, int]:
    """Разбирает RIFF-заголовок и оставляет поток на первом сэмпле."""
    riff = stream.read(12)
    if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise ValueError("это не WAV-поток")
    rate = channels = 0
    while True:
        head = stream.read(8)
        if len(head) < 8:
            raise ValueError("WAV-заголовок оборвался")
        chunk_id, size = struct.unpack("<4sI", head)
        if chunk_id == b"fmt ":
            fmt = stream.read(size)
            channels = struct.unpack_from("<H", fmt, 2)[0]
            rate = struct.unpack_from("<I", fmt, 4)[0]
        elif chunk_id == b"data":
            return rate, channels
        else:
            stream.read(size)


def _resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Передискретизация с подавлением зеркал.

    Голая интерполяция при 44.1 → 16 кГц заворачивает всё выше 8 кГц обратно
    в слышимый диапазон: шипящие превращаются в свист, и распознавание на этом
    спотыкается. Поэтому перед прореживанием усредняем скользящим окном —
    грубый, но честный ФНЧ, которого для речи достаточно.
    """
    if len(samples) == 0:
        return samples

    x = samples.astype(np.float32)
    if src > dst:
        window = max(1, int(round(src / dst)))
        if window > 1:
            kernel = np.ones(window, dtype=np.float32) / window
            x = np.convolve(x, kernel, mode="same")

    n_out = int(len(x) * dst / src)
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype(np.int16)


class BrowserSource:
    """Микрофон компьютера через вкладку пульта.

    Вкладка шлёт сырой PCM на веб-сервер робота (`POST /listen`), а мы
    забираем его непрерывным потоком с `GET /listen/stream`. Формат уже
    нужный — 16 кГц, моно, int16, — так что ни заголовков, ни
    передискретизации: браузер это делает лучше нас.
    """

    def __init__(self, web_url: str, sample_rate: int) -> None:
        self.url = web_url.rstrip("/") + "/listen/stream"
        self.sample_rate = sample_rate
        self.frame_len = sample_rate * FRAME_MS // 1000
        # Когда последний раз приходил НАСТОЯЩИЙ звук. Сервер робота держит
        # соединение живым, подсыпая ровные нули, и по «кадры идут» источник
        # выглядел живым всегда: робот бодро сообщал «микрофон на связи» и
        # при закрытой вкладке, и при выключенной кнопке микрофона.
        # Минус бесконечность, а не ноль: monotonic() считает от загрузки
        # машины, и с нулём первые пять секунд после включения выглядели бы
        # как «звук только что был» — то есть ровно то враньё, против
        # которого эта отметка и заведена. Сервис поднимается из systemd как
        # раз в эти секунды.
        self._real_at = float("-inf")

    def live(self) -> bool:
        """Идёт ли настоящий звук, а не одна тишина-заполнитель."""
        return time.monotonic() - self._real_at < REAL_AUDIO_TTL

    def frames(self) -> Iterator[np.ndarray]:
        import requests

        log.info("аудио: жду звук из браузера на %s", self.url)
        resp = requests.get(self.url, stream=True, timeout=(5, 30))
        resp.raise_for_status()

        tail = np.empty(0, dtype=np.int16)
        # Читаем ровно по кадру: с большим буфером тишина, которой сервер
        # держит соединение живым, копилась бы по несколько секунд.
        for buf in resp.iter_content(chunk_size=self.frame_len * 2):
            if not buf:
                continue
            # Ровные нули — это заполнитель сервера, а не звук: настоящий
            # микрофон всегда шумит хотя бы младшим битом.
            if any(buf):
                self._real_at = time.monotonic()
            samples = np.frombuffer(buf[: len(buf) // 2 * 2], dtype="<i2")
            tail = np.concatenate([tail, samples])
            n = len(tail) // self.frame_len
            for i in range(n):
                yield tail[i * self.frame_len:(i + 1) * self.frame_len]
            tail = tail[n * self.frame_len:]
        raise ConnectionError("поток из браузера оборвался")


def make_source(audio_source: str, phone_url: str, sample_rate: int,
                web_url: str = "http://127.0.0.1:8000", mic_device: str = ""):
    if audio_source == "local":
        return LocalSource(sample_rate, mic_device)
    if audio_source == "browser":
        return BrowserSource(web_url, sample_rate)
    return PhoneSource(phone_url, sample_rate)


# --------------------------------------------------------------------------
# Развязка чтения и обработки
# --------------------------------------------------------------------------
class Pump:
    """Читает источник в своём потоке, храня только свежий хвост звука.

    Без этого получается растущее отставание: пока Whisper думает пару секунд,
    телефон продолжает слать, данные копятся в сокете, и робот начинает
    отвечать на позавчерашние реплики. Здесь старые кадры просто выбрасываются —
    для диалога свежесть важнее полноты.
    """

    # Хвост должен пережить самый долгий ход модели: пока она думает, главный
    # цикл фраз не читает, и всё сказанное человеком копится здесь. Трёх
    # секунд, как было раньше, не хватало даже на быстрый ответ — «стоп»,
    # сказанный на ходу, выбрасывался, не будучи ни разу услышанным.
    # Тридцать секунд звука — это меньше мегабайта памяти.
    def __init__(self, source, keep_seconds: float = 30.0) -> None:
        self.source = source
        maxlen = max(10, int(keep_seconds * 1000 / FRAME_MS))
        self._frames: deque[np.ndarray] = deque(maxlen=maxlen)
        self._ready = threading.Condition()
        self._dropped = 0
        self._stop = False
        self._thread: threading.Thread | None = None
        # Живой ли сейчас источник — чтобы наверху было что показать человеку.
        self.online = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        with self._ready:
            self._ready.notify_all()

    def _run(self) -> None:
        """Читает источник, переподключаясь сколько понадобится.

        Обрыв звука — это норма, а не аварийная ситуация: телефон уходит в сон,
        Wi-Fi моргает, IP Webcam перезапускается. Раньше первая же такая ошибка
        пробрасывалась наверх и робот глох до ручного `systemctl restart` —
        поток пересоздавался, но старая ошибка так и оставалась записанной.
        Теперь переподключение живёт здесь, и наверх ничего не летит.
        """
        delay = 1.0
        while not self._stop:
            try:
                for frame in self.source.frames():
                    if self._stop:
                        return
                    if not self.online:
                        log.info("аудио: источник на связи")
                        self.online = True
                    delay = 1.0
                    with self._ready:
                        if len(self._frames) == self._frames.maxlen:
                            self._dropped += 1
                            # Каждые 500 кадров — это 10 секунд выброшенного
                            # звука. Обычно значит, что Whisper не успевает:
                            # рядом говорит телевизор и режется по 20 секунд.
                            if self._dropped % 500 == 0:
                                log.warning("аудио: не успеваю разбирать, "
                                            "выброшено %d кадров", self._dropped)
                        self._frames.append(frame)
                        self._ready.notify()
            except Exception as e:      # noqa: BLE001 — обрыв не повод умирать
                if self._stop:
                    return
                if self.online:
                    log.warning("аудио: источник пропал (%s), переподключаюсь", e)
                else:
                    log.debug("аудио: источник не отвечает (%s)", e)
                self.online = False
            # Паузу наращиваем, чтобы не долбить выключенный телефон каждую
            # секунду, но и не спать полминуты, когда он вернулся.
            with self._ready:
                self._ready.wait(timeout=delay)
            delay = min(10.0, delay * 2)

    def frames(self):
        while not self._stop:
            with self._ready:
                while not self._frames and not self._stop:
                    self._ready.wait(timeout=1.0)
                if not self._frames:
                    continue
                frame = self._frames.popleft()
            yield frame

    @property
    def alive(self) -> bool:
        """Идёт ли настоящий звук, а не только строки, держащие связь.

        Сам факт «кадры приходят» ничего не значит для браузерного источника:
        сервер робота подсыпает ровные нули, чтобы соединение не уснуло. Робот
        поэтому бодро сообщал «микрофон на связи» и при закрытой вкладке, и
        при выключенной кнопке микрофона.
        """
        live = getattr(self.source, "live", None)
        return self.online and (live() if callable(live) else True)

    def drop_pending(self) -> None:
        """Выбросить накопленное — например, всё, что робот наговорил сам."""
        with self._ready:
            self._frames.clear()

    def peek(self) -> bytes:
        """Копия накопленного, не забирая его из очереди.

        Нужна ровно для одного: пока робот думает над ответом, главный цикл
        висит в разговоре с моделью и фраз не читает. Если человек в этот
        момент сказал «стоп», а модель уже отправила робота ехать, узнать об
        этом больше неоткуда. Забирать кадры нельзя — они ещё пригодятся
        обычному разбору.
        """
        with self._ready:
            if not self._frames:
                return b""
            return b"".join(f.tobytes() for f in self._frames)


# --------------------------------------------------------------------------
# Нарезка на фразы
# --------------------------------------------------------------------------
class Listener:
    """Выдаёт по одной законченной фразе (WAV-байты) за раз."""

    def __init__(self, source, *, sample_rate: int, vad_level: int,
                 silence_ms: int, min_speech_ms: int, max_speech_ms: int = 20000,
                 start_frames: int = 2, spotter=None,
                 command_ms: int = 10000) -> None:
        self.pump = Pump(source)
        # Детектор имени на потоке. Есть — слушаем по имени и режем только
        # команду; нет — откатываемся на нарезку по паузам, см. utterances().
        self.spotter = spotter
        self.command_frames = max(1, command_ms // FRAME_MS)
        # Имя прозвучало в ЗВУКЕ, а не в тексте. Читается сразу после фразы —
        # как confidence у распознавания. Верх по этому признаку понимает, что
        # искать имя в расшифровке уже не надо: оно точно было.
        self.wake_heard = False
        # Кого дёрнуть, как только имя услышано. Нужно, чтобы приглушить
        # музыку ПРЯМО СЕЙЧАС, а не после распознавания: человек уже начал
        # говорить, и перекрикивать песню он не должен.
        self.on_wake: Callable[[], None] | None = None
        self.sample_rate = sample_rate
        self.vad = webrtcvad.Vad(vad_level)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self._min_speech = max(1, min_speech_ms // FRAME_MS)
        self.max_speech_frames = max(1, max_speech_ms // FRAME_MS)
        # Начинаем запись только после нескольких подряд речевых кадров:
        # одиночный щелчок или стук посуды VAD принимает за речь.
        self._start = max(1, start_frames)
        # Играет ли рядом музыка. Детектор речи её за речь и принимает: на
        # живом роботе под включённое радио каждая пауза уезжала в
        # распознавание отдельным куском, тот возвращался пустым, и полсекунды
        # ПК уходило в никуда — по нескольку раз в минуту.
        self._noisy = False
        # Небольшой предбуфер, чтобы не отрезать начало слова.
        self.preroll = deque(maxlen=max(1, 300 // FRAME_MS))
        self._muted = threading.Event()
        # Последний кусок пришлось резать по длине, а не по паузе. Читается
        # сразу после получения фразы — как confidence у распознавания.
        # Такой кусок — это срез комнаты, а не реплика: имя в нём может стоять
        # где угодно, и разбирать его надо иначе.
        self.long_chunk = False

    # Во сколько раз дольше должен звучать звук, чтобы под музыку считаться
    # началом фразы. Двух хватает: 40 мс подряд речевых кадров превращаются в
    # 80, а предбуфер в 300 мс всё равно донесёт начало слова целиком.
    NOISY = 2

    def background(self, шумно: bool) -> None:
        """Стало шумно или снова тихо. Зовётся, когда включают музыку."""
        if шумно != self._noisy:
            log.info("фон: %s", "играет музыка" if шумно else "тихо")
        self._noisy = шумно

    @property
    def start_frames(self) -> int:
        return self._start * self.NOISY if self._noisy else self._start

    @property
    def min_speech_frames(self) -> int:
        """Минимальная длина фразы. Под музыку НЕ поднимается — намеренно.

        Соблазн был: поднять оба порога и отсечь музыкальный мусор разом. Но
        этот порог отбраковывает уже записанный кусок по длине, а мусор под
        музыку был длинный — в логе куски по 2.6–3.6 секунды. То есть пользы
        никакой, а вред прямой: «стоп» — это меньше полусекунды речи, и
        удвоенный порог выбросил бы ровно ту команду, ради которой всё
        останавливается.
        """
        return self._min_speech

    # Пока робот говорит — не слушаем: иначе он расслышит сам себя.
    def mute(self) -> None:
        self._muted.set()

    def unheard(self) -> bytes:
        """Звук, который накопился, пока его никто не разбирал — как WAV.

        Отдаётся именно копия: разбор на фразы этот же звук ещё получит.
        Пусто — значит и слушать нечего.
        """
        raw = self.pump.peek()
        if not raw:
            return b""
        return to_wav(np.frombuffer(raw, dtype=np.int16), self.sample_rate)

    def unmute(self) -> None:
        self._muted.clear()
        self.preroll.clear()
        # Выбрасываем всё, что накопилось, пока робот говорил и думал.
        self.pump.drop_pending()

    def utterances(self) -> Iterator[bytes]:
        """Фразы для разбора. Как именно — решает наличие детектора имени."""
        if self.spotter is not None:
            yield from self._по_имени()
        else:
            yield from self._по_паузам()

    def _по_имени(self) -> Iterator[bytes]:
        """Слушаем имя на потоке, пишем только команду за ним.

        Пауз до имени не ждём вовсе — поэтому шумная комната и играющая музыка
        перестают мешать: детектор слушает поток как есть. Пауза нужна ровно
        для одного — понять, где команда кончилась.

        Имя в запись попадает не всегда: предбуфер короче слова, и начало
        может срезаться. Это не беда, а замысел — верх и не ищет имя в тексте,
        ему сказано звуком, что робота позвали.
        """
        self.pump.start()
        speech: list[np.ndarray] = []
        silence = 0
        пишем = False

        for frame in self.pump.frames():
            if self._muted.is_set():
                # Робот говорит сам. Копить его собственную речь в детекторе
                # нельзя: своё имя он произносит и сам, и позвал бы себя.
                speech, silence, пишем = [], 0, False
                self.preroll.clear()
                self.spotter.сбросить()
                continue

            if not пишем:
                self.preroll.append(frame)
                if self.spotter.услышал(frame.tobytes()):
                    log.info("аудио: имя услышано — слушаю команду")
                    if self.on_wake is not None:
                        try:
                            self.on_wake()
                        except Exception:       # noqa: BLE001 — музыка не повод глохнуть
                            log.exception("не смог приглушить музыку")
                    speech = list(self.preroll)
                    silence, пишем = 0, True
                continue

            speech.append(frame)
            if self.vad.is_speech(frame.tobytes(), self.sample_rate):
                silence = 0
            else:
                silence += 1

            # Либо человек договорил, либо говорит слишком долго. Предел здесь
            # щедрый: это длина ОДНОЙ команды, а не потолок задержки ответа,
            # как было у нарезки по паузам.
            if silence >= self.silence_frames or len(speech) >= self.command_frames:
                payload, speech = speech, []
                silence, пишем = 0, False
                self.preroll.clear()
                self.wake_heard = True
                yield to_wav(np.concatenate(payload), self.sample_rate)

    def _по_паузам(self) -> Iterator[bytes]:
        """Запасной способ: резать по паузам, имя искать в тексте.

        Работает без модели для ловли имени — хуже, но работает. Всё, что
        связано с «срезом комнаты», живёт только здесь: при живом детекторе
        куски приезжают размером с команду, и резать по длине незачем.
        """
        self.pump.start()
        speech: list[np.ndarray] = []
        silence = 0
        voiced_run = 0
        # Речевых кадров в куске — именно речевых, а не «всего минус хвост
        # тишины». Раньше считалось вторым способом, и в счёт попадали
        # предбуфер и семьсот миллисекунд тишины на конце: одиночный щелчок
        # набирал ровно порог и уезжал в распознавание как секунда почти
        # тишины. Порог при этом не работал вообще ни на чём.
        voiced_total = 0
        talking = False

        for frame in self.pump.frames():
            if self._muted.is_set():
                speech.clear()
                silence = voiced_run = voiced_total = 0
                talking = False
                continue

            is_speech = self.vad.is_speech(frame.tobytes(), self.sample_rate)

            if not talking:
                self.preroll.append(frame)
                voiced_run = voiced_run + 1 if is_speech else 0
                if voiced_run >= self.start_frames:
                    talking = True
                    speech = list(self.preroll)
                    silence = 0
                    voiced_total = voiced_run
                continue

            speech.append(frame)
            if is_speech:
                voiced_total += 1
            silence = 0 if is_speech else silence + 1

            # Либо человек замолчал, либо говорит слишком долго — режем.
            too_long = len(speech) >= self.max_speech_frames
            if too_long:
                # Раньше такой кусок выбрасывался целиком — считалось, что
                # столько подряд длится только шум. В живой квартире это
                # оказалось неверно: люди разговаривают непрерывно, паузы в
                # полсекунды не случается по двадцать секунд кряду, и вместе с
                # чужим разговором выбрасывалось обращение к роботу, если оно
                # прозвучало внутри. Человек звал — робот молчал.
                #
                # Теперь кусок уходит в разбор, но помечается: это срез
                # комнаты, а не реплика. Решать, по карману ли его разбирать,
                # будет верх — у него одного есть право знать, ПК сейчас
                # распознаёт или сам робот.
                log.info("аудио: %d с без паузы — режу и отдаю как есть",
                         self.max_speech_frames * FRAME_MS // 1000)
                payload, speech = speech, []
                voiced_total = silence = voiced_run = 0
                talking = False
                self.preroll.clear()
                self.long_chunk = True
                yield to_wav(np.concatenate(payload), self.sample_rate)
                continue

            if silence >= self.silence_frames:
                talking = False
                voiced_run = 0
                payload, speech = speech, []
                voiced, voiced_total = voiced_total, 0
                self.preroll.clear()
                if voiced >= self.min_speech_frames:
                    self.long_chunk = False
                    yield to_wav(np.concatenate(payload), self.sample_rate)
                else:
                    log.debug("аудио: слишком короткий фрагмент (%d кадров речи), "
                              "пропускаю", voiced)


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()
