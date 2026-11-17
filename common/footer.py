"""Футер с копирайтом для окон APrice (5.4): текст, стиль и клик с проверкой сайта.

Текст: ``АвтоПрайс 1.0 {Компонент}. Copyright (c) CoScore, 2026``. По клику на футер
проверяется доступность ``https://coscore.ru``; если сайт отвечает ``200 OK`` — он
открывается в браузере, иначе (сеть, таймаут, 404…) ничего не происходит. Проверка идёт
в фоновом потоке (таймаут 2 с), интерфейс не замирает.
"""

from __future__ import annotations

import logging
import webbrowser
from typing import Callable, Optional

import requests
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QLabel, QWidget

logger = logging.getLogger("aprice.footer")

APP_VERSION = "1.0"
COPYRIGHT_YEAR = 2026
COPYRIGHT_URL = "https://coscore.ru"
CHECK_TIMEOUT = 2.0
FOOTER_STYLE = "color: #888; font-size: 10px;"
#: Статусы, при которых сервер «не любит» HEAD — тогда пробуем GET (``stream=True``).
HEAD_NOT_ALLOWED = (403, 405, 501)


def footer_text(component: str) -> str:
    """Текст футера для компонента (``Client``, ``Server`` или ``Builder``)."""
    return f"АвтоПрайс {APP_VERSION} {component}. Copyright (c) CoScore, {COPYRIGHT_YEAR}"


def is_site_alive(url: str = COPYRIGHT_URL, timeout: float = CHECK_TIMEOUT) -> bool:
    """Отвечает ли сайт ``200 OK`` (редиректы разрешены). Любая ошибка — ``False``.

    Сначала лёгкий ``HEAD``; если сервер его не принимает (405 и т. п.) — ``GET`` с
    ``stream=True`` (тело не скачивается).
    """
    try:
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        if response.status_code in HEAD_NOT_ALLOWED:
            response = requests.get(url, timeout=timeout, allow_redirects=True, stream=True)
            try:
                return response.status_code == 200
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        return response.status_code == 200
    except requests.RequestException as exc:
        logger.debug("Сайт %s недоступен: %s", url, exc)
        return False
    except Exception:  # noqa: BLE001 - клик по футеру не должен ронять приложение
        logger.exception("Неожиданная ошибка при проверке %s", url)
        return False


class SiteCheckThread(QThread):
    """Проверяет сайт в фоне и сообщает результат сигналом ``checked(bool)``."""

    checked = pyqtSignal(bool)

    def __init__(self, url: str, checker: Callable[[str], bool] = is_site_alive, parent=None) -> None:
        """Запоминает адрес и функцию проверки (подменяется в тестах)."""
        super().__init__(parent)
        self._url, self._checker = url, checker

    def run(self) -> None:
        """Выполняет проверку; любое исключение трактуется как «сайт недоступен»."""
        try:
            alive = bool(self._checker(self._url))
        except Exception:  # noqa: BLE001
            alive = False
        self.checked.emit(alive)


class CopyrightFooter(QLabel):
    """Кликабельная строка копирайта: серый мелкий шрифт, по центру, курсор «рука»."""

    def __init__(
        self,
        component: str,
        parent: Optional[QWidget] = None,
        *,
        url: str = COPYRIGHT_URL,
    ) -> None:
        """Создаёт футер для компонента ``component`` (``Client``/``Server``/``Builder``)."""
        super().__init__(footer_text(component), parent)
        self._url = url
        self._thread: Optional[SiteCheckThread] = None
        self.setStyleSheet(FOOTER_STYLE)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(url)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - имя задано Qt
        """Левый щелчок запускает проверку сайта."""
        button = getattr(event, "button", lambda: Qt.MouseButton.LeftButton)()
        if button == Qt.MouseButton.LeftButton:
            self.activate()

    def activate(self) -> bool:
        """Запускает фоновую проверку сайта; ``False``, если предыдущая ещё идёт."""
        if self._thread is not None and self._thread.isRunning():
            return False
        self._thread = SiteCheckThread(self._url, is_site_alive, self)
        self._thread.checked.connect(self._on_checked)
        self._thread.start()
        return True

    def wait(self, timeout_ms: int = 5000) -> bool:
        """Дожидается окончания проверки (нужно тестам и при закрытии окна)."""
        return True if self._thread is None else bool(self._thread.wait(timeout_ms))

    def _on_checked(self, alive: bool) -> None:
        """Сайт жив (200) — открыть в браузере; иначе тихо ничего не делать."""
        if alive:
            try:
                webbrowser.open(self._url)
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось открыть %s", self._url)
