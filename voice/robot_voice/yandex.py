"""Яндекс.Музыка: поиск, исполнители и «Моя волна».

Интернет-радио решает половину задачи: музыка играет, но какая — решает
станция. «Кузя, поставь Цоя» радио не умеет в принципе. Яндекс умеет, а
подписка Плюс в доме уже есть — значит, платить второй раз не придётся.

Библиотека `yandex-music` неофициальная: своего API Яндекс не публиковал,
протокол разобрали по приложению. Отсюда два следствия, о которых честнее
помнить заранее. Первое: обновление на стороне Яндекса может сломать это в
любой день — поэтому радио никуда не убирается и остаётся тем, что играет,
когда музыка не отвечает. Второе: токен живёт вне репозитория, в
~/.robot-ai.env, рядом с ключом от облака.

Ссылки на треки подписанные и живут недолго: сама библиотека предупреждает,
что информация о загрузке действительна минуту. Поэтому очередь мы храним
треками, а ссылку добываем ровно перед тем, как включить, — заранее
нарезанный плейлист протух бы на третьей песне.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

# «Моя волна» — та самая бесконечная станция, которая подстраивается под
# лайки. Для «Кузя, включи музыку» это ровно то, что нужно: не надо ничего
# выбирать, а с каждым разом попадает точнее.
WAVE = "user:onyourwave"

# Размера партии здесь нет намеренно: ротор сам отдаёт по пять и ждёт, что мы
# вернёмся за следующей. Постоянная BATCH = 5 стояла тут описанием этого факта,
# но её никто не читал — wave() размер партии не передаёт вовсе.

# Сколько треков брать из поиска: «поставь Цоя» — это не одна песня, а вечер.
FOUND = 15

# Браузер играет mp3 всегда и везде. У Яндекса есть и aac, и он лучше по
# качеству, но на этом мы уже обожглись с радио: станция нашлась, адрес уехал
# в пульт, робот отрапортовал «включаю» — и тишина.
CODEC = "mp3"


class Music:
    """Тонкая обёртка над клиентом Яндекса. Наружу — только треки и ссылки.

    Всё, что может сломаться, ломается внутри и наружу выходит пустотой:
    голосовой цикл не должен падать оттого, что у Яндекса сменился протокол.
    """

    def __init__(self, token: str = "") -> None:
        self.token = (token or "").strip()
        self._client = None
        self._broken = ""
        # Клиента дёргают и главный поток (по команде голосом), и поток
        # проигрывателя (когда кончился трек). Requests-сессия внутри одна.
        self._lock = threading.Lock()
        self._stations: list[tuple[str, str]] = []

    # --- подключение ----------------------------------------------------
    @staticmethod
    def installed() -> bool:
        """Стоит ли библиотека. Проверка без импорта и без сети.

        Нужна ровно для одного: сказать правду при старте. Раньше робот
        сообщал «музыка: Яндекс и радио» по одному факту наличия токена, а про
        отсутствующую библиотеку признавался только в момент первой просьбы
        включить музыку — и то строкой ниже в логе. На живом роботе на этом
        потерялся круг: токен получен, робот отрапортовал, что Яндекс есть, а
        play_music отвечал «не нашёл».
        """
        import importlib.util

        return importlib.util.find_spec("yandex_music") is not None

    @property
    def possible(self) -> bool:
        """Есть ли смысл вообще пробовать: токен задан и библиотека стоит."""
        return bool(self.token) and not self._broken

    def _connect(self):
        """Клиент Яндекса или None. Ошибку показываем один раз, не в цикле."""
        if self._client is not None or not self.possible:
            return self._client
        try:
            from yandex_music import Client
        except ImportError:
            self._broken = "библиотека yandex-music не установлена"
            log.info("Яндекс.Музыка выключена: %s", self._broken)
            return None
        try:
            client = Client(self.token).init()
        except Exception as e:                      # noqa: BLE001
            # Протухший токен, отозванная подписка, лежащий Яндекс — всё сюда.
            # Дальше молчим до перезапуска: долбить чужой сервис на каждой
            # фразе незачем, а радио продолжает работать.
            self._broken = str(e)
            log.warning("Яндекс.Музыка не пустила: %s", e)
            return None
        имя = getattr(getattr(client, "me", None), "account", None)
        log.info("Яндекс.Музыка на связи%s",
                 f" ({имя.display_name})" if имя and имя.display_name else "")
        self._client = client
        return client

    # --- треки ----------------------------------------------------------
    def search(self, query: str, limit: int = FOUND) -> list:
        """Треки по запросу: имя исполнителя, название песни, что угодно.

        Сначала спрашиваем исполнителя: «поставь Цоя» — это про все его песни,
        а поиск по трекам вернул бы каверы и трибьюты вперемешку.
        """
        client = self._connect()
        if client is None:
            return []
        with self._lock:
            try:
                найдено = client.search(query, type_="all")
            except Exception as e:                  # noqa: BLE001
                log.warning("поиск в Яндексе не вышел: %s", e)
                return []
            треки = self._by_artist(client, найдено, limit)
            if треки:
                return треки
            подходят = getattr(getattr(найдено, "tracks", None), "results", None)
            return _playable(подходят or [])[:limit]

    def _by_artist(self, client, найдено, limit: int) -> list:
        """Популярные песни исполнителя, если запрос — это имя.

        Совпадение требуем точное. Иначе «поставь спокойную музыку» находит
        группу «Спокойной ночи» и весь вечер играет она.
        """
        артисты = getattr(getattr(найдено, "artists", None), "results", None)
        спросили = " ".join((getattr(найдено, "text", "") or "").lower().split())
        for артист in (артисты or [])[:1]:
            if (артист.name or "").lower() != спросили:
                return []
            try:
                хиты = client.artists_tracks(артист.id, page_size=limit)
            except Exception as e:                  # noqa: BLE001
                log.debug("треки исполнителя не пришли: %s", e)
                return []
            треки = _playable(getattr(хиты, "tracks", None) or [])[:limit]
            if треки:
                log.info("Яндекс: исполнитель %s, треков %d", артист.name, len(треки))
            return треки
        return []

    # --- станции --------------------------------------------------------
    def station(self, query: str) -> str:
        """Идентификатор жанровой станции по русскому названию. Пусто — нет.

        Жанры Яндекс называет по-русски и сам («Джаз», «Русский рок»), так что
        сопоставлять слова со списком тегов вручную не нужно — достаточно
        спросить у него список и найти вхождение.
        """
        client = self._connect()
        if client is None or not query:
            return ""
        query = " ".join(query.lower().split())
        with self._lock:
            if not self._stations:
                try:
                    for результат in client.rotor_stations_list(language="ru") or []:
                        станция = результат.station
                        if станция and станция.id:
                            self._stations.append((
                                (станция.name or "").lower(),
                                f"{станция.id.type}:{станция.id.tag}"))
                except Exception as e:              # noqa: BLE001
                    log.debug("список станций не пришёл: %s", e)
                    return ""
        for имя, ident in self._stations:
            if имя == query:
                return ident
        for имя, ident in self._stations:
            if query in имя or имя in query:
                return ident
        return ""

    def wave(self, station: str = WAVE, after: str = "") -> tuple[list, str]:
        """Очередная партия треков станции и номер партии.

        `after` — трек, на котором мы остановились: без него ротор честно
        отдаёт одно и то же начало по кругу.
        """
        client = self._connect()
        if client is None:
            return [], ""
        with self._lock:
            try:
                ответ = client.rotor_station_tracks(station, queue=after or None)
            except Exception as e:                  # noqa: BLE001
                log.warning("станция %s не ответила: %s", station, e)
                return [], ""
        куски = getattr(ответ, "sequence", None) or []
        треки = _playable([к.track for к in куски if к.track])
        return треки, getattr(ответ, "batch_id", "") or ""

    def started(self, station: str, batch: str = "") -> None:
        """Сказать ротору, что станцию включили. Без этого он глохнет.

        Обратная связь для «Моей волны» — не вежливость, а часть протокола:
        станция подбирает следующее по тому, что доиграли до конца, а что
        перемотали. Ошибку глушим — на музыку она не влияет.
        """
        self._feedback("rotor_station_feedback_radio_started",
                       station, from_="robot-kuzya", batch_id=batch)

    def playing(self, station: str, track_id: str, batch: str = "") -> None:
        self._feedback("rotor_station_feedback_track_started",
                       station, track_id=track_id, batch_id=batch)

    def played(self, station: str, track_id: str, seconds: float,
               batch: str = "") -> None:
        self._feedback("rotor_station_feedback_track_finished",
                       station, track_id=track_id,
                       total_played_seconds=seconds, batch_id=batch)

    def _feedback(self, method: str, station: str, **kwargs) -> None:
        client = self._connect()
        if client is None or not station:
            return
        with self._lock:
            try:
                getattr(client, method)(station, **kwargs)
            except Exception as e:                  # noqa: BLE001
                log.debug("отзыв станции не ушёл (%s): %s", method, e)

    # --- ссылка ---------------------------------------------------------
    def link(self, track) -> str:
        """Прямая ссылка на mp3. Пусто — трек не отдали.

        Ссылка подписанная и живёт недолго, поэтому зовётся это ровно перед
        включением, а не при сборке очереди.
        """
        client = self._connect()
        if client is None:
            return ""
        with self._lock:
            try:
                варианты = track.get_download_info(get_direct_links=True)
            except Exception as e:                  # noqa: BLE001
                log.warning("ссылку на трек не дали: %s", e)
                return ""
        годные = [в for в in варианты or []
                  if (в.codec or "").lower() == CODEC and в.direct_link]
        if not годные:
            log.warning("у трека «%s» нет mp3 — пропускаю", title(track))
            return ""
        # Из mp3 берём самый жирный: 320 против 192 на колонках слышно.
        годные.sort(key=lambda в: в.bitrate_in_kbps or 0, reverse=True)
        return годные[0].direct_link


def _playable(tracks) -> list:
    """Только то, что и правда дадут послушать.

    Без подписки Яндекс отдаёт тридцать секунд превью, а недоступные треки
    (ушёл правообладатель, региональный запрет) приходят в выдаче наравне с
    остальными и молча не играют.
    """
    годные = []
    for трек in tracks or []:
        if трек is None or getattr(трек, "available", True) is False:
            continue
        годные.append(трек)
    return годные


def title(track) -> str:
    """«Исполнитель — Название». Это же робот произносит вслух."""
    название = (getattr(track, "title", "") or "").strip()
    try:
        артисты = track.artists_name()
    except Exception:                               # noqa: BLE001
        артисты = []
    кто = ", ".join(a for a in artists_short(артисты) if a)
    return f"{кто} — {название}" if кто and название else (название or кто or "трек")


def artists_short(names: list[str], limit: int = 2) -> list[str]:
    """Первые двое исполнителей. Читать вслух список из восьми — мучение."""
    names = [n for n in (names or []) if n]
    return names[:limit]
