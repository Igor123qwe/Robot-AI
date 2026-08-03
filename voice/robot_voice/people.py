"""Личные дела: что робот знает о каждом, с кем разговаривает.

Разделение труда с ПК намеренное. Слепки голосов лежат там, где считаются, —
на компьютере: это двести чисел на человека и модель на восемьдесят мегабайт.
А личные дела живут здесь, на роботе, потому что нужны ему в разговоре — в том
числе когда компьютер выключен и разговор идёт через облако.

Что в деле. Имя, сколько раз разговаривали, когда виделись в последний раз и
список фактов: что человек любит, чем занят, о чём просил не забыть. Факты
кладёт сама модель, когда человек что-то о себе сказал, — и она же их читает
в следующий раз. Из этого и получается «помнит меня», а не «отвечает всем
одинаково».

Всё хранится на роботе, в его же папке данных, и стирается голосом:
«забудь про меня» — и дела нет.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Сколько фактов держим на человека. Больше — это уже не личное дело, а
# протокол наблюдения: и в промпт не влезет, и оплачивается в каждой реплике.
FACTS_LIMIT = 12

# Сколько символов от факта берём. Модель иногда пишет абзац там, где нужна
# строка, а промпт уезжает в каждом запросе.
FACT_LENGTH = 160


class People:
    """Личные дела всех, кого робот знает."""

    def __init__(self, store: Path) -> None:
        self.store = store
        self._lock = threading.Lock()
        self.cards: dict[str, dict] = {}
        try:
            self.cards = json.loads(store.read_text("utf-8"))
        except (OSError, ValueError):
            self.cards = {}

    def _save(self) -> None:
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cards, ensure_ascii=False, indent=1),
                           "utf-8")
            tmp.replace(self.store)
        except OSError as e:
            log.warning("не сохранил личные дела (%s)", e)

    def card(self, name: str) -> dict:
        return self.cards.get(name) or {}

    def known(self) -> list[str]:
        return sorted(self.cards)

    def met(self, name: str) -> None:
        """Отмечает встречу. Зовётся на каждой узнанной фразе."""
        if not name:
            return
        with self._lock:
            card = self.cards.setdefault(name, {"факты": [], "разговоров": 0})
            card["разговоров"] = card.get("разговоров", 0) + 1
            card["виделись"] = datetime.now().isoformat(timespec="seconds")
            self._save()

    def remember(self, name: str, fact: str) -> str:
        """Записывает факт о человеке. Возвращает, что сказать вслух."""
        fact = " ".join(fact.split())[:FACT_LENGTH]
        if not name:
            return "Я пока не знаю, кто ты. Скажи «запомни мой голос»."
        if not fact:
            return "А что запомнить?"
        with self._lock:
            card = self.cards.setdefault(name, {"факты": [], "разговоров": 0})
            facts = card.setdefault("факты", [])
            # Дубли не копим: человек повторяет одно и то же, а платим мы за
            # каждое повторение в каждом следующем запросе.
            if any(f.lower() == fact.lower() for f in facts):
                return "Это я уже про тебя знаю."
            facts.append(fact)
            del facts[:-FACTS_LIMIT]
            self._save()
        log.info("про %s запомнил: %s", name, fact)
        return "Запомнил."

    def forget(self, name: str) -> str:
        if not name or name not in self.cards:
            return "Я про тебя ничего и не помню."
        with self._lock:
            del self.cards[name]
            self._save()
        log.info("личное дело %s стёрто", name)
        return "Всё стёр."

    def tell(self, name: str) -> str:
        """Что робот скажет вслух на «что ты обо мне знаешь»."""
        card = self.card(name)
        if not name:
            return "Пока не знаю, кто ты."
        facts = card.get("факты") or []
        if not facts:
            return f"Знаю, что тебя зовут {name}. Больше пока ничего."
        return "Про тебя знаю вот что. " + " ".join(
            f[:1].upper() + f[1:].rstrip(".") + "." for f in facts)

    def brief(self, name: str) -> str:
        """Короткая справка для модели. Уезжает в каждом запросе, поэтому сжато.

        Кладётся отдельным системным блоком, а не в основной промпт: тот байт
        в байт одинаков в каждом запросе и оплачивается по льготной цене как
        кэш. Справка меняется от человека к человеку и кэшу только помешала бы.
        """
        if not name:
            return ""
        card = self.card(name)
        facts = card.get("факты") or []
        who = f"Сейчас с тобой говорит {name}."
        if not facts:
            return who
        return who + " Что ты о нём знаешь: " + "; ".join(facts) + "."
