"""Статистика цен для APrice-Client (подзадача 4.2). Модуль без Qt.

:func:`calculate_stats` считает пять обязательных показателей: минимум, максимум,
среднее, медиану и усечённое среднее (α = 10 %).

Решения и допущения:

* Некорректные цены (``None``, не числа, ``NaN``/``inf``, ``bool``, ``<= 0``)
  **исключаются** до расчёта — это защита от сбоев парсеров. Исключённые учитываются
  в ``ignored`` и в ``warning``.
* ``trimmed_mean``: ``k = int(N * 0.10)`` элементов отбрасывается с каждого конца
  отсортированного списка. При ``N < 10`` ``k == 0``, и усечённое среднее равно
  обычному среднему (отбрасывать нечего) — расчёт при этом сопровождается
  предупреждением «Мало данных». Ветка ``k * 2 >= N`` из ТЗ оставлена как защита
  (при ``0.10`` она достижима только для ``N == 0``).
* ``warning`` — одна строка; несколько причин склеиваются через пробел.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

import numpy as np

#: Доля, отбрасываемая с каждого конца при усечённом среднем.
TRIM_ALPHA = 0.10
#: Меньше стольких цен — оценка считается неточной.
MIN_RELIABLE_COUNT = 10

WARNING_NO_DATA = "Нет данных для оценки."
WARNING_FEW = "Мало данных (N < 10). Оценка может быть неточной."
WARNING_TRIM = "Слишком мало данных для усечённого среднего"
WARNING_IGNORED = "Исключено некорректных цен: {n}."


def _is_valid_price(value: Any) -> bool:
    """Цена — конечное положительное число (``bool`` числом не считается)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        return False
    return math.isfinite(float(value)) and value > 0


def calculate_stats(prices: Iterable[Any]) -> dict[str, Any]:
    """Считает показатели по списку цен.

    Args:
        prices: цены объявлений (числа); некорректные значения исключаются.

    Returns:
        dict: ``count`` (число учтённых цен), ``min``, ``max`` (``int``, если все цены
        целые), ``mean``, ``median``, ``trimmed_mean`` (``float``), ``ignored`` (сколько
        значений исключено) и ``warning`` (``str`` или ``None``). Показатели — ``None``,
        когда данных нет.
    """
    raw = list(prices)
    valid = sorted(p for p in raw if _is_valid_price(p))
    ignored = len(raw) - len(valid)
    count = len(valid)

    warnings: list[str] = []
    result: dict[str, Any] = {
        "count": count,
        "min": None,
        "max": None,
        "mean": None,
        "median": None,
        "trimmed_mean": None,
        "ignored": ignored,
        "warning": None,
    }
    if count == 0:
        warnings.append(WARNING_NO_DATA)
    else:
        result["min"] = valid[0]
        result["max"] = valid[-1]
        result["mean"] = float(np.mean(valid))
        result["median"] = float(np.median(valid))
        trim = int(count * TRIM_ALPHA)
        if trim * 2 >= count:
            warnings.append(WARNING_TRIM)
        else:
            kept = valid[trim: count - trim]
            result["trimmed_mean"] = float(np.mean(kept))
        if count < MIN_RELIABLE_COUNT:
            warnings.append(WARNING_FEW)
    if ignored:
        warnings.append(WARNING_IGNORED.format(n=ignored))
    result["warning"] = " ".join(warnings) if warnings else None
    return result
