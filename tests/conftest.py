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

#: Тестовая «четвёртая площадка»: реальных площадок три (bibika.ru отключена), а
#: тесты планировщика проверяют арифметику round-robin на четырёх.
FOURTH_STUB_SITE = "stub4.test"


@pytest.fixture()
def fourth_stub_site(monkeypatch) -> str:
    """Регистрирует тестовую площадку-заглушку ``stub4.test`` (парсер и контракт).

    Подключается модулями планировщика через ``pytestmark``; в реальных реестрах
    и константах площадки нет.
    """
    from server import protocol
    from server.parsers import PARSER_REGISTRY
    from server.parsers.base import BaseSiteParser

    class FourthStubParser(BaseSiteParser):
        site_name = FOURTH_STUB_SITE

        def collect(self, spec, options, stop_event, progress_cb=None, on_page=None):
            return self._collect_stub(spec, options, stop_event, progress_cb, on_page)

    monkeypatch.setitem(PARSER_REGISTRY, FOURTH_STUB_SITE, FourthStubParser)
    monkeypatch.setattr(protocol, "ALLOWED_SITES", protocol.ALLOWED_SITES | {FOURTH_STUB_SITE})
    return FOURTH_STUB_SITE


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    """Единственный ``QApplication`` тестовой сессии (не закрывается в teardown)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    yield app
