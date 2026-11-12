"""Общие утилиты проекта «АвтоПрайс» (APrice).

Содержит:

* :func:`interruptible_sleep` — прерываемую пассивную паузу, которой
  пользуются и парсеры площадок (``BaseSiteParser``), и планировщик
  ``TaskManager`` (S-12: остановка сбора должна действовать почти мгновенно);
* :func:`format_eta` — человекочитаемое оставшееся время (трей и окно
  прогресса Server, S-09/S-10; позже — окно прогресса Client).
"""

from __future__ import annotations

import time
from threading import Event

#: Интервал проверки ``stop_event`` во время паузы по умолчанию, сек (S-12).
STOP_CHECK_INTERVAL_SEC: float = 0.5


def interruptible_sleep(
    stop_event: Event,
    timeout: float,
    check_interval: float = STOP_CHECK_INTERVAL_SEC,
) -> bool:
    """Пауза длительностью ``timeout`` сек, прерываемая ``stop_event``.

    Пауза разбивается на интервалы не длиннее ``check_interval``; каждый
    интервал выполняется через ``stop_event.wait()``, поэтому установка
    события прерывает паузу немедленно. Длительность отсчитывается по
    ``time.monotonic()`` — смена системного времени на неё не влияет.

    Args:
        stop_event: событие остановки.
        timeout: длительность паузы, сек (значения ``<= 0`` — без ожидания).
        check_interval: максимальная длина одного ожидания, сек.

    Returns:
        bool: ``True`` — пауза прошла полностью; ``False`` — прервана
        установкой ``stop_event`` (в том числе до начала паузы).
    """
    if stop_event.is_set():
        return False
    chunk_limit = check_interval if check_interval > 0 else STOP_CHECK_INTERVAL_SEC
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return not stop_event.is_set()
        if stop_event.wait(min(chunk_limit, remaining)):
            return False


def format_eta(seconds: int | float | None) -> str:
    """Форматирует оставшееся время в человекочитаемую строку.

    Минуты округляются **вниз** (оценка «~» и так приблизительная).
    ``None`` и отрицательные значения трактуются как «меньше минуты».

    Примеры:
        >>> format_eta(45)
        'меньше минуты'
        >>> format_eta(90)
        '~1 мин'
        >>> format_eta(3700)
        '~1 ч 1 мин'
        >>> format_eta(7200)
        '~2 ч'

    Args:
        seconds: оставшееся время, сек.

    Returns:
        str: ``"меньше минуты"``, ``"~N мин"``, ``"~H ч"`` или ``"~H ч M мин"``.
    """
    if seconds is None or seconds < 60:
        return "меньше минуты"
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"~{minutes} мин"
    if minutes == 0:
        return f"~{hours} ч"
    return f"~{hours} ч {minutes} мин"
