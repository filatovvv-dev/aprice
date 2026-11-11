"""Общие утилиты проекта «АвтоПрайс» (APrice).

Сейчас содержит :func:`interruptible_sleep` — прерываемую пассивную паузу,
которой пользуются и парсеры площадок (``BaseSiteParser``), и планировщик
``TaskManager`` (S-12: остановка сбора должна действовать почти мгновенно).
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
