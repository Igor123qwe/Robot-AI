"""Голосовой ассистент домашнего робота.

Точка входа импортируется лениво: иначе любой импорт пакета — например ради
одних инструментов — тянул бы за собой numpy, webrtcvad и faster-whisper.
"""

__all__ = ["main"]


def main() -> None:
    from .app import main as _main
    _main()
