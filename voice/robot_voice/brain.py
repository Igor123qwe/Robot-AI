"""Диалог с моделью: история, вызов инструментов, потоковый ответ.

Цикл «модель → инструмент → модель» написан руками, а не отдан tool_runner из
SDK. Причина в том, что робот может ходить не только к api.anthropic.com, но и
через сторонний роутер: tool_runner живёт на beta-эндпоинте, а прокси такое
обычно не пропускает. Обычный /v1/messages поддерживается везде.

Собеседников может быть двое. Основной — модель на домашнем ПК: она отвечает
за секунду, не стоит денег и работает без интернета. Запасной — облако: умнее,
но платное и медленное. Робот сам ходит к тому, кто отвечает: ПК выключили —
разговор продолжается через облако, и человек этого даже не замечает.

Ответ отдаётся кусками, чтобы синтез речи начинался, не дожидаясь конца
генерации.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import anthropic

from .config import SYSTEM_PROMPT, Config
from .tools import Tool

log = logging.getLogger(__name__)

# Сколько сообщений диалога держим в контексте (реплики плюс результаты
# инструментов). Для голосового робота длинная память не нужна: разговор идёт
# короткими репликами, а каждое лишнее сообщение оплачивается в каждом
# следующем запросе. Восемь — это примерно четыре обмена.
HISTORY_LIMIT = 8

# Предохранитель от зацикливания: модель не должна дёргать инструменты вечно.
MAX_TOOL_ROUNDS = 6

# Сколько ждём ответа. Умолчание SDK — десять минут: для голосового робота это
# вечность, потому что микрофон всё это время заглушён, а если модель успела
# вызвать drive — робот в это время едет и не слышит «стоп».
TIMEOUT_SECONDS = 25.0

# Сколько ждём, пока установится связь с ПК. Читать ответ можно долго —
# генерация идёт секунды, — а вот дозваниваться нельзя: если ПК выключен или
# спит, каждая фраза упрётся в это ожидание, и робот будет молчать вместо того
# чтобы ответить через облако.
CONNECT_SECONDS = 2.0

# Столько не трогаем собеседника, который не отозвался. Иначе робот будет
# проверять выключенный ПК на каждой фразе и каждый раз терять на этом время.
DOWN_SECONDS = 60.0


def _timeout(connect: float | None):
    """Таймаут запроса: связь коротко, чтение долго.

    Без httpx (так бывает в самопроверке, где клиент модели подменён
    заглушкой) отдаём одно число — SDK его тоже понимает.
    """
    if connect is None:
        return TIMEOUT_SECONDS
    try:
        import httpx
    except ImportError:
        return TIMEOUT_SECONDS
    return httpx.Timeout(TIMEOUT_SECONDS, connect=connect)


@dataclass
class Endpoint:
    """Один собеседник: куда идти, какой моделью и во сколько это обходится."""

    name: str
    client: object
    model: str
    effort: str
    # Локальная модель ничего не стоит, и мешать её токены с облачными нельзя:
    # счётчик расхода тогда перестанет показывать деньги.
    paid: bool = True
    # Кэширование постоянной части промпта. Облако его понимает, локальная
    # модель — почти наверняка нет: отключим на первом же отказе.
    use_cache: bool = True
    # До какого момента не беспокоить: он только что не отозвался.
    down_until: float = 0.0
    # Свой счётчик расхода — чтобы видеть, сколько разговора ушло мимо кассы.
    spent_in: int = 0
    spent_out: int = 0


class Brain:
    def __init__(self, cfg: Config, tools: list[Tool]) -> None:
        self.cfg = cfg
        self.tools = {t.name: t for t in tools}
        # Модели показываем не всё: постоянная часть промпта уезжает в каждом
        # запросе и оплачивается каждый раз. Правила закрывают таймеры,
        # список, громкость и прочее — модели эти схемы возить незачем.
        self.specs = [t.spec() for t in tools if not t.hidden]

        self.endpoints: list[Endpoint] = []
        if cfg.local_api_base:
            # Основной. Ключ тут формальность: своя машина в своей сети пароля
            # не спрашивает, но SDK без ключа работать отказывается.
            self.endpoints.append(Endpoint(
                name=f"ПК ({cfg.local_api_base})",
                client=self._client(cfg.local_api_key, cfg.local_api_base,
                                    CONNECT_SECONDS),
                model=cfg.local_model,
                # Маленькая модель этого параметра не знает, и слать его
                # незачем: первый же запрос уйдёт в отказ и потеряет секунду.
                effort="",
                paid=False,
                use_cache=False,
            ))
        self.endpoints.append(Endpoint(
            name=cfg.api_base or "api.anthropic.com",
            client=self._client(cfg.api_key, cfg.api_base, None),
            model=cfg.model,
            effort=cfg.effort,
        ))

        self.history: list[dict] = []
        # Расход с момента запуска — чтобы видеть цену вопроса, а не гадать.
        self.total_in = 0
        self.total_out = 0
        # Когда в последний раз говорили. Держим здесь, а не в цикле: цикл
        # перезапускается после сбоя, а разговор от этого не свежеет.
        self.last_talk = 0.0

    @staticmethod
    def _client(key: str, base: str, connect: float | None):
        args = {
            "api_key": key,
            "timeout": _timeout(connect),
            # Две попытки: сетевой сбой бывает, но ждать третью робот не может.
            "max_retries": 1,
        }
        if base:
            args["base_url"] = base
        return anthropic.Anthropic(**args)

    # --- выбор собеседника ------------------------------------------------
    def _live(self) -> list[Endpoint]:
        """Кого сейчас имеет смысл спрашивать, в порядке предпочтения."""
        now = time.monotonic()
        live = [e for e in self.endpoints if e.down_until <= now]
        # Если отдыхают все — облако всё равно пробуем: лучше подождать, чем
        # отказать человеку, пока идёт отсчёт.
        return live or self.endpoints[-1:]

    # --- параметры запроса ----------------------------------------------
    def _params(self, ep: Endpoint, messages: list[dict]) -> dict:
        params = dict(
            model=ep.model,
            max_tokens=self.cfg.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.specs,
            messages=messages,
        )
        # effort понимают не все: у роутера или у сторонней модели его может
        # не быть. Пустое значение — не отправлять.
        if ep.effort:
            params["output_config"] = {"effort": ep.effort}

        if ep.use_cache:
            # Промпт и схемы инструментов байт в байт одинаковы в каждом
            # запросе — около 1300 токенов, которые незачем оплачивать заново.
            # Отметка ставится на системный блок: инструменты идут перед ним,
            # поэтому кэшируются вместе с ним.
            params["system"] = [{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }]
        return params

    def _degrade(self, ep: Endpoint, error: Exception) -> bool:
        """Отключает необязательный параметр, который не понял собеседник.

        Ни сторонний роутер, ни модель на ПК могут не знать ни кэширования,
        ни effort. Раньше отступление стояло вокруг вызова messages.stream(),
        а он запрос не делает — HTTP уходит только при входе в with. Поэтому
        отступление не срабатывало никогда, и робот отвечал «что-то пошло не
        так» на каждый разговорный вопрос до конца жизни процесса.
        """
        if ep.use_cache:
            log.warning("%s: кэширование промпта не принято (%s) — работаю без него",
                        ep.name, error)
            ep.use_cache = False
            return True
        if ep.effort:
            log.warning("%s: параметр effort не принят (%s) — убираю", ep.name, error)
            ep.effort = ""
            return True
        return False

    def _ask(self, ep: Endpoint, messages: list[dict], on_text: Callable[[str], None]):
        """Один запрос к одному собеседнику."""
        while True:
            try:
                with ep.client.messages.stream(**self._params(ep, messages)) as stream:
                    for event in stream:
                        if (event.type == "content_block_delta"
                                and getattr(event.delta, "type", "") == "text_delta"):
                            on_text(event.delta.text)
                    return stream.get_final_message()
            except anthropic.BadRequestError as e:
                if not self._degrade(ep, e):
                    raise

    def _round(self, messages: list[dict], on_text: Callable[[str], None]):
        """Один запрос к модели: отдаёт текст кусками, возвращает ответ целиком.

        Молчащего собеседника обходим и идём к следующему. Возвращаем ещё и
        того, кто ответил: от этого зависит, писать ли расход в деньги.
        """
        said = False

        def watch(chunk: str) -> None:
            nonlocal said
            said = True
            on_text(chunk)

        failure: Exception | None = None
        for ep in self._live():
            try:
                return ep, self._ask(ep, messages, watch)
            except anthropic.APIConnectionError as e:
                failure = e
                if said:
                    # Часть ответа уже прозвучала вслух. Начинать заново
                    # нельзя: человек услышит начало фразы дважды.
                    raise
                ep.down_until = time.monotonic() + DOWN_SECONDS
                log.warning("%s не отвечает (%s) — иду к следующему", ep.name, e)
        raise failure

    # --- один ход --------------------------------------------------------
    def reply(self, user_text: str, on_text: Callable[[str], None]) -> str:
        """Отвечает на реплику. on_text вызывается на каждый кусок текста."""
        messages = self.history + [{"role": "user", "content": user_text}]
        spoken: list[str] = []
        used_in = used_out = cached = written = 0
        truncated = False

        def collect(chunk: str) -> None:
            spoken.append(chunk)
            on_text(chunk)

        rounds = 0
        answered: Endpoint | None = None
        for rounds in range(1, MAX_TOOL_ROUNDS + 1):
            answered, message = self._round(messages, collect)

            usage = getattr(message, "usage", None)
            if usage is not None:
                used_in += getattr(usage, "input_tokens", 0) or 0
                used_out += getattr(usage, "output_tokens", 0) or 0
                cached += getattr(usage, "cache_read_input_tokens", 0) or 0
                # Запись в кэш тоже оплачивается и в input_tokens не попадает:
                # без неё счётчик врал в меньшую сторону каждый раз, когда
                # промпт клался в кэш заново.
                written += getattr(usage, "cache_creation_input_tokens", 0) or 0

            if message.stop_reason == "max_tokens":
                truncated = True
                log.warning("ответ обрезан по лимиту в %d токенов", self.cfg.max_tokens)

            if message.stop_reason == "refusal":
                log.warning("модель отказалась отвечать: %s",
                            getattr(message, "stop_details", None))
                return "Извини, на это я ответить не могу."

            messages.append({"role": "assistant", "content": message.content})

            calls = [b for b in message.content if b.type == "tool_use"]
            if not calls:
                break

            results = []
            for call in calls:
                tool = self.tools.get(call.name)
                if tool is None:
                    log.warning("модель зовёт несуществующий инструмент %s", call.name)
                    output = f"Инструмента {call.name} не существует."
                else:
                    log.info("вызываю %s(%s)", call.name, call.input)
                    output = tool(call.input or {})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                })
            # Все результаты — одним сообщением, иначе модель разучится
            # вызывать инструменты пачкой.
            messages.append({"role": "user", "content": results})
        else:
            log.warning("исчерпан лимит вызовов инструментов за один ход (%d)", rounds)

        if answered is not None:
            answered.spent_in += used_in + written
            answered.spent_out += used_out
            # В общий счёт идёт только платное. Иначе цифра «всего за сеанс»
            # перестанет означать деньги, а ради неё она и заведена.
            if answered.paid:
                self.total_in += used_in + written
                self.total_out += used_out
        details = []
        if cached:
            details.append(f"из кэша {cached}")
        if written:
            details.append(f"в кэш {written}")
        log.info("отвечал %s | токены: %d вход + %d выход%s | платных за сеанс %d + %d",
                 answered.name if answered else "никто",
                 used_in + written, used_out,
                 f" ({', '.join(details)})" if details else "",
                 self.total_in, self.total_out)

        if truncated:
            # Иначе робот замолкает на полуслове, и понять почему нельзя:
            # человек думает, что он отвлёкся, и повторяет вопрос.
            collect(" Дальше не помещаюсь, спроси покороче.")

        self.history = _trim(messages)
        return "".join(spoken).strip()

    def reset(self) -> None:
        self.history.clear()


def _trim(messages: list[dict]) -> list[dict]:
    """Оставляет хвост истории, не разрывая пару «вызов инструмента — ответ».

    Резать можно только по настоящей реплике человека: сообщение с tool_result
    без предшествующего tool_use API отклонит.

    Если в последних HISTORY_LIMIT сообщениях реплики человека нет — а так
    бывает после хода с четырьмя и более вызовами инструментов, — ищем её
    дальше вглубь. Раньше в этом случае возвращалась вся история целиком, и
    она росла линейно: каждый тяжёлый ход добавлял по десятку сообщений,
    которые оплачивались в каждом следующем запросе.
    """
    if len(messages) <= HISTORY_LIMIT:
        return messages
    starts = [i for i, m in enumerate(messages)
              if m.get("role") == "user" and isinstance(m.get("content"), str)]
    # Самая ранняя реплика человека, после которой остаётся не больше лимита.
    for i in starts:
        if len(messages) - i <= HISTORY_LIMIT:
            return messages[i:]
    # Все ходы длиннее лимита — берём последний. Реплика человека в истории
    # есть всегда: с неё начинается каждый ход.
    return messages[starts[-1]:]
