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


def _real_qt_canvas_available() -> bool:
    """``True``, если matplotlib умеет Qt-холст (есть настоящие биндинги PyQt6)."""
    try:
        import matplotlib.backends.backend_qtagg  # noqa: F401
    except ImportError:
        return False
    return True


_QT_CANVAS = _real_qt_canvas_available()


@pytest.fixture(autouse=True)
def chart_canvas(monkeypatch):
    """Без настоящего Qt-холста (CI без PyQt6) подставляет виджет-пустышку.

    На обычной машине с PyQt6 ничего не подменяется: тесты используют
    ``FigureCanvasQTAgg``. Рисование (``draw_charts``) в любом случае настоящее.
    """
    if _QT_CANVAS:
        return
    from PyQt6.QtWidgets import QWidget
    from client import charts

    monkeypatch.setattr(charts, "_make_canvas", lambda figure: QWidget())
