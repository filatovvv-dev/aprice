"""Тесты общих утилит (``common.utils``) — подзадача 3.4a (rev 2).

Главное, что фиксируют тесты: контракт возвращаемого значения
``interruptible_sleep`` — ``True`` при полной паузе, ``False`` при прерывании
(в прежней версии метода ``BaseSiteParser`` значение было инвертировано).
"""

from __future__ import annotations

import threading
import time

from common.utils import interruptible_sleep


def test_full_pause_returns_true() -> None:
    """Без остановки пауза проходит полностью и возвращает True."""
    stop = threading.Event()
    started = time.monotonic()
    assert interruptible_sleep(stop, 0.2) is True
    assert time.monotonic() - started >= 0.19


def test_zero_or_negative_timeout_returns_immediately() -> None:
    """Нулевая/отрицательная пауза — без ожидания, True."""
    stop = threading.Event()
    started = time.monotonic()
    assert interruptible_sleep(stop, 0) is True
    assert interruptible_sleep(stop, -5) is True
    assert time.monotonic() - started < 0.1


def test_event_already_set_returns_false_immediately() -> None:
    """Если событие уже установлено — сразу False, даже для нулевой паузы."""
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    assert interruptible_sleep(stop, 10) is False
    assert interruptible_sleep(stop, 0) is False
    assert time.monotonic() - started < 0.1


def test_interrupted_during_pause_returns_false_quickly() -> None:
    """Установка события посреди длинной паузы прерывает её почти мгновенно."""
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    started = time.monotonic()
    assert interruptible_sleep(stop, 30) is False
    assert time.monotonic() - started < 2.0


def test_custom_check_interval_is_accepted() -> None:
    """Параметр check_interval не меняет результат (в т.ч. нулевой/малый)."""
    stop = threading.Event()
    assert interruptible_sleep(stop, 0.05, check_interval=0.01) is True
    assert interruptible_sleep(stop, 0.05, check_interval=0) is True
