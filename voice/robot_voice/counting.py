"""Счёт, секундомер и жребий — то, чего языковая модель не умеет честно.

Три вещи, которые дома спрашивают каждый день, а модель отвечает на них плохо
по разным причинам.

Счёт. «Пятнадцать процентов от двух тысяч» модель считает в уме и иногда
ошибается — не в сложении, а в процентах и делении. Ошибка при этом выглядит
ровно так же уверенно, как правильный ответ, и проверить её на слух нельзя.

Секундомер. Модель не знает, сколько прошло времени: у неё нет часов между
репликами. Спросить «сколько там натикало» было некому.

Жребий. Вот это важнее всего: модель не умеет бросать монетку. Она выдаёт то,
что чаще встречалось в тексте, и на просьбу «загадай число от одного до
десяти» отвечает семёркой заметно чаще прочего. Для игры с ребёнком это не
случайность, а подделка под неё.
"""

from __future__ import annotations

import ast
import operator
import random
import time

from . import ru

# --- счёт ------------------------------------------------------------------

# Считаем разбором выражения, а не eval: eval на строке из микрофона — это
# запуск чего угодно, что услышал робот. Здесь же работают только операции из
# этого списка, а всё остальное просто не имеет смысла в дереве разбора.
_ОПЕРАЦИИ = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Больше этого в быту не спрашивают, а вот «два в степени десять тысяч» вешает
# счёт намертво: число просто не помещается в память.
ПРЕДЕЛ_СТЕПЕНИ = 1000


def _счесть(узел):
    if isinstance(узел, ast.Constant):
        if isinstance(узел.value, bool) or not isinstance(узел.value, (int, float)):
            raise ValueError("не число")
        return узел.value
    if isinstance(узел, ast.BinOp) and type(узел.op) in _ОПЕРАЦИИ:
        слева, справа = _счесть(узел.left), _счесть(узел.right)
        if isinstance(узел.op, ast.Pow) and abs(справа) > ПРЕДЕЛ_СТЕПЕНИ:
            raise ValueError("слишком большая степень")
        return _ОПЕРАЦИИ[type(узел.op)](слева, справа)
    if isinstance(узел, ast.UnaryOp) and type(узел.op) in _ОПЕРАЦИИ:
        return _ОПЕРАЦИИ[type(узел.op)](_счесть(узел.operand))
    raise ValueError("непонятное выражение")


def посчитать(выражение: str) -> float:
    """Число или ValueError. Деление на ноль тоже ValueError — так удобнее."""
    выражение = (выражение or "").strip()
    if not выражение or len(выражение) > 200:
        raise ValueError("пусто или слишком длинно")
    try:
        дерево = ast.parse(выражение, mode="eval")
    except SyntaxError as e:
        raise ValueError("не разобрал выражение") from e
    try:
        ответ = _счесть(дерево.body)
    except ZeroDivisionError as e:
        raise ValueError("делить на ноль нельзя") from e
    except (OverflowError, MemoryError) as e:
        raise ValueError("слишком большое число") from e
    if isinstance(ответ, complex) or ответ != ответ or ответ in (float("inf"), float("-inf")):
        raise ValueError("не число")
    return ответ


def вслух(число: float) -> str:
    """Число так, как его произносят: без хвоста из нулей и с запятой."""
    if число == int(число) and abs(число) < 10 ** 15:
        return str(int(число))
    округлённое = round(число, 4)
    return f"{округлённое:.4f}".rstrip("0").rstrip(".").replace(".", ",")


# --- секундомер ------------------------------------------------------------


class Секундомер:
    """Пущен, остановлен, сколько натикало. Переживает паузу, но не перезапуск.

    Хранить на диске незачем: секундомер заводят на суп и на игру, а не на
    неделю. Зато остановленный не забывается — «сколько там» после «стоп»
    отвечает тем же числом, а не нулём.
    """

    def __init__(self, часы=time.monotonic) -> None:
        self._часы = часы
        self._пущен: float | None = None
        self._накоплено = 0.0

    @property
    def идёт(self) -> bool:
        return self._пущен is not None

    @property
    def секунд(self) -> float:
        сверх = (self._часы() - self._пущен) if self._пущен is not None else 0.0
        return self._накоплено + сверх

    def пуск(self) -> str:
        if self._пущен is not None:
            return f"Секундомер уже идёт, {self.вслух()}."
        self._пущен = self._часы()
        return "Засёк." if self._накоплено else "Пошёл."

    def стоп(self) -> str:
        if self._пущен is None:
            if self._накоплено:
                return f"Секундомер и так стоит, {self.вслух()}."
            return "Секундомер не запущен."
        self._накоплено = self.секунд
        self._пущен = None
        return f"Остановил, {self.вслух()}."

    def сброс(self) -> str:
        было = self._накоплено or self.секунд
        self._пущен, self._накоплено = None, 0.0
        return "Сбросил." if было else "Секундомер и так на нуле."

    def вслух(self) -> str:
        секунд = self.секунд
        if секунд < 1:
            return "меньше секунды"
        return ru.duration(round(секунд))

    def сколько(self) -> str:
        if self._пущен is None and not self._накоплено:
            return "Секундомер не запущен."
        идёт = "идёт" if self._пущен is not None else "остановлен"
        return f"Секундомер {идёт}: {self.вслух()}."


# --- жребий ----------------------------------------------------------------

СТОРОНЫ = ("Орёл", "Решка")


def монетка(бросок=None) -> str:
    выбор = (бросок or random.choice)(list(СТОРОНЫ))
    return f"{выбор}."


def кубик(граней: int = 6, бросок=None) -> str:
    граней = max(2, min(100, int(граней or 6)))
    выпало = (бросок or random.randint)(1, граней)
    return f"{выпало}." if граней == 6 else f"{выпало} из {граней}."


def число(снизу: int = 1, сверху: int = 10, бросок=None) -> str:
    снизу, сверху = int(снизу), int(сверху)
    if снизу > сверху:
        снизу, сверху = сверху, снизу
    выпало = (бросок or random.randint)(снизу, сверху)
    return f"{выпало}."


def выбрать(из_чего: list[str], бросок=None) -> str:
    варианты = [с.strip() for с in из_чего if с and с.strip()]
    if not варианты:
        return "Не из чего выбирать."
    if len(варианты) == 1:
        return f"Выбирать не из чего, только {варианты[0]}."
    return f"{(бросок or random.choice)(варианты)}."
