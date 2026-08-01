"""Список покупок и дел — то, что у ассистентов просят каждый день.

Живёт в обычном JSON рядом с таймерами и переживает перезапуск сервиса.
Никакого облака: список нужен на кухне, а не в интернете.

Один список намеренно: «покупки» и «дела» в быту всё равно произносятся
вперемешку, а разбираться, куда положить «купить хлеб», робот будет хуже
человека. Если понадобится разделение — здесь появится поле kind.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

LIMIT = 100   # больше сотни пунктов вслух всё равно не прочитать


class Notes:
    def __init__(self, store: Path | None = None) -> None:
        self._items: list[str] = []
        self._lock = threading.Lock()
        self.store = store
        self._load()

    # --- работа со списком ----------------------------------------------
    def add(self, item: str) -> bool:
        """Добавляет пункт. False — такой уже есть."""
        item = _clean(item)
        if not item:
            return False
        with self._lock:
            if _same(item, self._items) is not None:
                return False
            if len(self._items) >= LIMIT:
                self._items.pop(0)
            self._items.append(item)
            self._save()
        return True

    def remove(self, item: str) -> str | None:
        """Убирает пункт. Возвращает то, что убрал, или None."""
        item = _clean(item)
        with self._lock:
            found = _same(item, self._items)
            if found is None:
                return None
            self._items.remove(found)
            self._save()
        return found

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._save()
        return count

    def items(self) -> list[str]:
        with self._lock:
            return list(self._items)

    # --- хранение --------------------------------------------------------
    def _load(self) -> None:
        if self.store is None or not self.store.exists():
            return
        try:
            data = json.loads(self.store.read_text())
        except (OSError, ValueError) as e:
            log.warning("не смог прочитать список: %s", e)
            return
        if isinstance(data, list):
            self._items = [str(x) for x in data][:LIMIT]

    def _save(self) -> None:
        """Замок уже держит вызывающий."""
        if self.store is None:
            return
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_name(self.store.name + ".tmp")
            tmp.write_text(json.dumps(self._items, ensure_ascii=False))
            tmp.replace(self.store)
        except OSError as e:
            log.warning("не смог сохранить список: %s", e)


def _clean(item: str) -> str:
    return " ".join(item.split()).strip(" ,.!?;:-")[:60]


def _same(item: str, items: list[str]) -> str | None:
    """Ищет тот же пункт независимо от регистра и ё/е.

    «Молоко» и «молоко» — это один пункт, иначе список за неделю зарастёт
    дублями: Whisper пишет с большой буквы не всегда одинаково.
    """
    key = item.lower().replace("ё", "е")
    for existing in items:
        if existing.lower().replace("ё", "е") == key:
            return existing
    return None
