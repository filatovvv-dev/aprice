"""Общие фикстуры тестов APrice.

Единственный ``QApplication`` на всю сессию: он подходит и GUI-тестам (трей,
диалоги), и не-GUI тестам (IPC, TaskManager). «Голые» core-приложения в тестах
не создаются: Qt допускает один экземпляр на процесс, и такое
приложение блокировало бы создание ``QApplication`` в других модулях.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    """Единственный ``QApplication`` тестовой сессии (не закрывается в teardown)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    yield app
