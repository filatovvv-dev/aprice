"""Состояние формы ввода и построение ``spec`` для Server (подзадача 4.1).

Модуль **без Qt**: окно только читает виджеты в :class:`FormState`, а проверка
готовности и сборка ``spec`` — чистые функции, которые тестируются без GUI.

Единственный режим — диапазонный (``spec.mode == "range"``, 4.6): диапазоны годов и
объёма, КПП и топливо — списки. Точный год задаётся как «Год от» = «Год до».
Режим «Точный автомобиль» из Client удалён; Server по-прежнему принимает
``mode: "exact"`` (контракт :mod:`server.protocol`), но Client его не отправляет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

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

    manufacturer: str = ""
    model: str = ""
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    volume_from: Optional[float] = None
    volume_to: Optional[float] = None
    transmissions: list[str] = field(default_factory=list)
    fuels: list[str] = field(default_factory=list)


def form_error(state: FormState) -> Optional[str]:
    """Причина, по которой форму нельзя отправить; ``None`` — форма готова.

    Обязательны марка и модель и корректные диапазоны.
    """
    if not state.manufacturer.strip():
        return "Выберите марку"
    if not state.model.strip():
        return "Выберите модель"
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
        "mode": MODE_RANGE,
        "manufacturer": state.manufacturer.strip(),
        "model": state.model.strip(),
        "year_from": int(state.year_from),  # type: ignore[arg-type]
        "year_to": int(state.year_to),  # type: ignore[arg-type]
        "transmissions": list(state.transmissions),
        "fuels": list(state.fuels),
    }
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
