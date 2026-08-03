#!/usr/bin/env python3
"""Разовый вход в Яндекс.Музыку: получает токен и кладёт его в ~/.robot-ai.env.

Запускать на роботе, один раз:

    voice/.venv/bin/python voice/yandex_auth.py

Именно питоном из venv: этим же питоном запускается служба, и библиотека,
поставленная системному, ей не видна. Скрипт это проверяет и не даст записать
токен туда, откуда его некому будет прочитать.

Скрипт покажет короткий код и адрес страницы Яндекса. Открываешь страницу на
телефоне или компьютере, вводишь код, подтверждаешь — и всё, токен записан.
Пароль при этом нигде не вводится и никуда не передаётся: это обычный OAuth
для устройств, тот же, что у телевизоров и колонок.

Токен на экран не печатается и в историю команд не попадает — он сразу
уезжает в ~/.robot-ai.env, который лежит вне репозитория. Права на файл
ставим 600: рядом с ним живёт ключ от облака.

Токен долгоживущий, но не вечный. Перестала играть музыка, а в логе
«Яндекс.Музыка не пустила» — просто запусти этот скрипт ещё раз.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ENV = Path(os.environ.get("ROBOT_ENV_FILE") or (Path.home() / ".robot-ai.env"))
KEY = "YANDEX_MUSIC_TOKEN"

# Так робот подпишется в списке устройств в аккаунте Яндекса — чтобы через
# полгода было понятно, что это, и чтобы отозвать доступ можно было точно.
DEVICE = "Кузя, домашний робот"


VENV = Path(__file__).resolve().parent / ".venv"


def _venv_pip() -> str:
    """Команда установки — та, что и правда сработает на этом роботе."""
    pip = VENV / "bin" / "pip"
    return str(pip) if pip.exists() else "pip3"


def _wrong_python() -> str:
    """Питон службы, если запустились не им. Пусто — всё правильно."""
    служебный = VENV / "bin" / "python"
    if not служебный.exists():
        return ""                       # venv нет, значит и путать не с чем
    try:
        свой = Path(sys.executable).resolve()
    except OSError:
        return ""
    return "" if свой == служебный.resolve() else str(служебный)


def показать(code) -> None:
    print()
    print("  Открой:", code.verification_url)
    print("  Введи код:", code.user_code)
    print()
    print(f"  Жду подтверждения (до {code.expires_in // 60} минут)…", flush=True)


def записать(token: str) -> None:
    """Кладёт токен в env-файл, не показывая его и не плодя дубли."""
    строки = []
    if ENV.exists():
        строки = ENV.read_text(encoding="utf-8").splitlines()
    строки = [s for s in строки if not s.strip().startswith(f"{KEY}=")]
    строки.append(f"{KEY}={token}")
    ENV.write_text("\n".join(строки) + "\n", encoding="utf-8")
    ENV.chmod(stat.S_IRUSR | stat.S_IWUSR)


def main() -> int:
    try:
        from yandex_music import Client
    except ImportError:
        print("Нет библиотеки. Поставь её тем же питоном, каким запускается",
              file=sys.stderr)
        print("служба, — иначе она её не увидит:", file=sys.stderr)
        print(f"    {_venv_pip()} install yandex-music", file=sys.stderr)
        return 1

    # Библиотека есть, но у ТОГО ли питона. Токен, записанный из системного
    # питона, служба прочитает, а библиотеку — нет: она живёт в venv. На живом
    # роботе это выглядело как «токен получен, робот говорит, что Яндекс
    # подключён, и не находит ничего».
    чужой = _wrong_python()
    if чужой:
        print(f"\nОсторожно: ты запустил это {sys.executable},", file=sys.stderr)
        print(f"а служба работает {чужой}.", file=sys.stderr)
        print("Токен-то запишется, но библиотеку служба не увидит. Поставь её",
              file=sys.stderr)
        print(f"и туда:\n    {_venv_pip()} install yandex-music", file=sys.stderr)

    print("Вход в Яндекс.Музыку для робота.")
    print("Нужна активная подписка Плюс: без неё Яндекс отдаёт только")
    print("тридцать секунд от каждой песни.")

    try:
        token = Client().device_auth(on_code=показать, device_name=DEVICE)
    except KeyboardInterrupt:
        print("\nОтменил.", file=sys.stderr)
        return 1
    except Exception as e:                          # noqa: BLE001
        print(f"\nНе вышло: {e}", file=sys.stderr)
        return 1

    записать(token.access_token)
    print(f"\nГотово, токен записан в {ENV}.")
    print("Осталось перезапустить голос:")
    print("    sudo systemctl restart robot-voice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
