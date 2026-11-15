"""Состояние формы ввода и построение ``spec`` для Server (подзадача 4.1).

Модуль **без Qt**: окно только читает виджеты в :class:`FormState`, а проверка
готовности и сборка ``spec`` — чистые функции, которые тестируются без GUI.

Режимы (``spec.mode`` контракта :mod:`server.protocol`):

* ``exact`` — «Точный автомобиль» (режим А): один год выпуска, необязательные
  поколение, КПП и топливо;
* ``range`` — «Прицениться» (режим Б): диапазоны годов и объёма, КПП и топливо —
  списки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

MODE_EXACT = "exact"
MODE_RANGE = "range"

#: Варианты КПП/топлива, если в справочнике их нет (так они пишутся в объявлениях).
TRANSMISSIONS: tuple[str, ...] = ("АКПП", "МКПП", "Вариатор", "Робот")
FUELS: tuple[str, ...] = ("Бензин", "Дизель", "Гибрид", "Электро")

MIN_YEAR = 1950


def max_year() -> int:
    """Верхняя граница года в форме: текущий + 1 (модельный год)."""
    return date.today().year + 1


@dataclass
class FormState:
    """Значения формы. Пустые строки/``None`` — «не выбрано / любое»."""

    mode: str = MODE_EXACT
    manufacturer: str = ""
    model: str = ""
    # режим А
    generation: str = ""
    year: Optional[int] = None
    transmission: str = ""
    fuel: str = ""
    # режим Б
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    volume_from: Optional[float] = None
    volume_to: Optional[float] = None
    transmissions: list[str] = field(default_factory=list)
    fuels: list[str] = field(default_factory=list)


def form_error(state: FormState) -> Optional[str]:
    """Причина, по которой форму нельзя отправить; ``None`` — форма готова.

    Обязательны марка и модель (оба режима). В режиме А нужен ещё год (он всегда
    выбран по умолчанию), в режиме Б — корректные диапазоны.
    """
    if not state.manufacturer.strip():
        return "Выберите марку"
    if not state.model.strip():
        return "Выберите модель"
    if state.mode == MODE_EXACT:
        if state.year is None:
            return "Выберите год выпуска"
    elif state.mode == MODE_RANGE:
        if state.year_from is None or state.year_to is None:
            return "Укажите диапазон годов"
        if state.year_from > state.year_to:
            return "Год «от» больше года «до»"
        if (
            state.volume_from is not None
            and state.volume_to is not None
            and state.volume_from > state.volume_to
        ):
            return "Объём «от» больше объёма «до»"
    else:
        return f"Неизвестный режим: {state.mode!r}"
    return None


def is_ready(state: FormState) -> bool:
    """``True``, если форму можно отправлять (кнопка «Оценить» активна)."""
    return form_error(state) is None


def build_spec(state: FormState) -> dict:
    """Собирает ``spec`` для ``submit_task`` по контракту ``server.protocol``.

    Raises:
        ValueError: форма не готова (см. :func:`form_error`).
    """
    error = form_error(state)
    if error is not None:
        raise ValueError(error)

    spec: dict = {
        "mode": state.mode,
        "manufacturer": state.manufacturer.strip(),
        "model": state.model.strip(),
    }
    if state.mode == MODE_EXACT:
        spec["year_from"] = spec["year_to"] = int(state.year)  # type: ignore[arg-type]
        if state.generation:
            spec["generation"] = state.generation
        spec["transmissions"] = [state.transmission] if state.transmission else []
        spec["fuels"] = [state.fuel] if state.fuel else []
    else:
        spec["year_from"] = int(state.year_from)  # type: ignore[arg-type]
        spec["year_to"] = int(state.year_to)  # type: ignore[arg-type]
        spec["transmissions"] = list(state.transmissions)
        spec["fuels"] = list(state.fuels)
        if state.volume_from is not None:
            spec["engine_volume_from"] = float(state.volume_from)
        if state.volume_to is not None:
            spec["engine_volume_to"] = float(state.volume_to)
    return spec


def build_options() -> dict:
    """Опции задания: пусто — Server применяет свои значения по умолчанию.

    Площадки берутся из ``server.default_sites``, лимиты — из контракта.
    """
    return {}
