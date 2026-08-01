"""Мелкие настройки, которые меняются голосом и должны пережить перезапуск.

Пока это только громкость, но такого добра будет больше: автообновление
перезапускает сервис каждые две минуты при любом изменении в репозитории, и
всё, что человек настроил голосом, иначе откатывается само собой — причём
ночью, когда он этого не ждёт.

Файл общий и крошечный, поэтому пишется целиком.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class State:
    def __init__(self, store: Path | None = None) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()
        self.store = store
        if store is not None and store.exists():
            try:
                loaded = json.loads(store.read_text())
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, ValueError) as e:
                log.warning("не смог прочитать настройки: %s", e)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            if self.store is None:
                return
            try:
                self.store.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.store.with_name(self.store.name + ".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False))
                tmp.replace(self.store)
            except OSError as e:
                log.warning("не смог сохранить настройки: %s", e)
