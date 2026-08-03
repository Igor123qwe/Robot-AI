"""Личные дела: что робот знает о каждом, с кем разговаривает.

Разделение труда с ПК намеренное. Слепки голосов лежат там, где считаются, —
на компьютере: это двести чисел на человека и модель на восемьдесят мегабайт.
А личные дела живут здесь, на роботе, потому что нужны ему в разговоре — в том
числе когда компьютер выключен и разговор идёт через облако.

Что в деле. Имя, сколько раз разговаривали, когда виделись в последний раз и
факты: что человек любит, чем занят, о чём просил не забыть. Из них и
получается «помнит меня», а не «отвечает всем одинаково».

Факты двух сортов, и разница между ними важна.

Записанные по просьбе — «запомни, что я работаю по ночам». Это прямое
поручение, и вытеснять его нельзя.

Подмеченные — то, что робот услышал в разговоре сам, без просьбы. Так и
получается настоящий конспект: человек не диктует анкету, он просто
разговаривает. Но такие заметки и врут чаще, и накапливаются быстрее, поэтому
при нехватке места первыми уходят они, а не поручения.

Всё хранится на роботе, в его же папке данных, и стирается голосом:
«забудь про меня» — и дела нет.
"""

from __future__ import annotations

import json
import logging
import re
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

# Короче этого — не факт, а обрывок: «да», «ага», «чай». Такое в деле только
# место занимает и путает модель в следующий раз.
FACT_MINIMUM = 8

# Слова короче этого при сравнении записей не учитываем: «и», «в», «не» —
# это не про смысл, а про грамматику.
SHORT_WORD = 3


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

    def remember(self, name: str, fact: str, *, asked: bool = True) -> str:
        """Записывает факт о человеке. Возвращает, что сказать вслух.

        asked=False — робот подметил это сам, без просьбы. Такие записи и
        врут чаще, и накапливаются быстрее, поэтому вытесняются первыми.
        """
        fact = " ".join(fact.split())[:FACT_LENGTH]
        if not name:
            return ("Я пока не знаю, кто ты. Скажи «запомни мой голос»."
                    if asked else "Не знаю, кто говорит — записывать некуда.")
        if len(fact) < FACT_MINIMUM:
            return "А что запомнить?" if asked else "Слишком коротко, не записал."
        with self._lock:
            card = self.cards.setdefault(name, {"факты": [], "разговоров": 0})
            facts = _as_records(card.get("факты"))
            same = _find_same(facts, fact)
            if same is not None:
                changed = False
                # Подробнее прежнего — берём новую формулировку: «любит чай»
                # уступает «любит крепкий чай по утрам».
                if len(fact) > len(same.get("что", "")):
                    same["что"] = fact
                    changed = True
                # Раньше это было мимолётным наблюдением, а теперь человек
                # попросил запомнить — запись становится поручением, и
                # вытеснять её больше нельзя.
                if asked and same.get("сам"):
                    same["сам"] = False
                    changed = True
                if changed:
                    card["факты"] = facts
                    self._save()
                return "Это я уже про тебя знаю."
            facts.append({"что": fact, "сам": not asked,
                          "когда": datetime.now().date().isoformat()})
            card["факты"] = _trimmed(facts)
            self._save()
        log.info("про %s %s: %s", name,
                 "запомнил по просьбе" if asked else "подметил сам", fact)
        return "Запомнил." if asked else "Записал в память, вслух не говори."

    def forget(self, name: str) -> str:
        if not name or name not in self.cards:
            return "Я про тебя ничего и не помню."
        with self._lock:
            del self.cards[name]
            self._save()
        log.info("личное дело %s стёрто", name)
        return "Всё стёр."

    def facts(self, name: str) -> list[str]:
        """Что робот знает о человеке, по порядку записи."""
        return [r["что"] for r in _as_records(self.card(name).get("факты"))]

    def tell(self, name: str) -> str:
        """Что робот скажет вслух на «что ты обо мне знаешь»."""
        if not name:
            return "Пока не знаю, кто ты."
        facts = self.facts(name)
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
        facts = self.facts(name)
        who = f"Сейчас с тобой говорит {name}."
        if not facts:
            return who
        return who + " Что ты о нём знаешь: " + "; ".join(facts) + "."


def _as_records(facts) -> list[dict]:
    """Приводит факты к единому виду.

    Раньше они лежали простыми строками. Читать старое дело мы обязаны: у
    человека там записи, которые он роботу диктовал, и терять их из-за смены
    формата нельзя.
    """
    out = []
    for f in facts or []:
        out.append(f if isinstance(f, dict) else {"что": str(f), "сам": False})
    return out


def _words(text: str) -> set[str]:
    """Значимые слова фразы. Пунктуация и мелочь вроде «и», «в» отброшены."""
    bare = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    # Числа держим любой длины: «встреча в 10» и «встреча в 15» — это разные
    # записи, а по длине слова они обе отсеялись бы как мелочь.
    return {w for w in bare.split() if len(w) >= SHORT_WORD or w.isdigit()}


def _find_same(facts: list[dict], fact: str) -> dict | None:
    """Уже записанное про то же самое, если есть.

    Считаем повтором только два случая: слова совпали полностью (пусть и в
    другом порядке) или одна запись целиком содержится в другой — «любит чай»
    и «любит крепкий чай по утрам». Второе особенно нужно роботу, который
    подмечает сам: он возвращается к той же теме и дописывает подробности.

    По похожести букв не сравниваем намеренно. «Любит чай» и «любит кофе»
    отличаются на одно слово и совпадают на восемьдесят процентов букв — а
    слить их в одну запись значит потерять то, ради чего дело и заводилось.
    """
    mine = _words(fact)
    if not mine:
        return None
    for record in facts:
        known = _words(record.get("что", ""))
        if known and (known == mine or known <= mine or mine <= known):
            return record
    return None


def _trimmed(facts: list[dict]) -> list[dict]:
    """Оставляет FACTS_LIMIT записей, выбрасывая сначала подмеченные.

    Порядок важен. Поручение «запомни, что я работаю по ночам» человек дал
    сознательно, и вытеснять его болтовнёй нельзя. А подмеченное роботом —
    догадка: не сбылась, так и бог с ней.
    """
    if len(facts) <= FACTS_LIMIT:
        return facts
    лишних = len(facts) - FACTS_LIMIT
    keep, dropped = [], 0
    for record in facts:
        if dropped < лишних and record.get("сам"):
            dropped += 1
            continue
        keep.append(record)
    # Одних подмеченных не хватило — режем старейшее, каким бы оно ни было.
    return keep[len(keep) - FACTS_LIMIT:] if len(keep) > FACTS_LIMIT else keep
