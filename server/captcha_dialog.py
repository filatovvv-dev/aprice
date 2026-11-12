"""Диалог CAPTCHA APrice-Server (S-13, A-08, W-03).

:class:`CaptchaDialog` предлагает пользователю открыть страницу площадки в
браузере для ручного прохождения CAPTCHA либо пропустить площадку.

Решения (задокументированы по CD-01, CD-06):

* **Модальность (CD-06).** Диалог **немодальный** (``setModal(False)``):
  сервер продолжает обрабатывать сигналы и работу других площадок, а
  показ диалога не требует вложенного цикла событий.
* **Центрирование (CD-01, W-03).** Позиция всегда по центру и не
  сохраняется. ``QMessageBox`` дорасчитывает размер уже после показа,
  поэтому центрирование выполняется в ``showEvent`` и повторно через
  ``QTimer.singleShot(0, …)``.
* **Закрытие (CD-07).** Крестик и ``Esc`` равнозначны «Пропустить сайт».
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import QMessageBox, QWidget

from common.window_manager import WindowManager

TITLE = "Обнаружена CAPTCHA"
TEXT = "На площадке {site} требуется прохождение CAPTCHA.\nОткрыть страницу в браузере?"
BUTTON_OPEN = "Открыть браузер"
BUTTON_SKIP = "Пропустить сайт"


class CaptchaDialog(QMessageBox):
    """Диалог «Обнаружена CAPTCHA» с кнопками «Открыть браузер» / «Пропустить сайт».

    Signals:
        open_browser_requested(url): пользователь выбрал «Открыть браузер».
        skip_site_requested: «Пропустить сайт» (в т.ч. крестик/``Esc``).
    """

    open_browser_requested = pyqtSignal(str)
    skip_site_requested = pyqtSignal()

    def __init__(
        self,
        site: str,
        url: str,
        parent: Optional[QWidget] = None,
        window_manager: Optional[WindowManager] = None,
    ) -> None:
        """Создаёт диалог.

        Args:
            site: площадка, на которой обнаружена CAPTCHA.
            url: адрес страницы для открытия в браузере.
            parent: родительский виджет.
            window_manager: менеджер окон для ``center_error``; по умолчанию
                используется статический ``WindowManager.center`` (то же
                центрирование, без файла настроек и побочных эффектов).
        """
        super().__init__(parent)
        self._site = site
        self._url = url
        self._window_manager = window_manager
        self._decided = False

        self.setIcon(QMessageBox.Icon.Warning)
        self.setWindowTitle(TITLE)
        self.setText(TEXT.format(site=site))
        self.setModal(False)  # CD-06

        self.button_open = self.addButton(
            BUTTON_OPEN, QMessageBox.ButtonRole.AcceptRole
        )
        self.button_skip = self.addButton(
            BUTTON_SKIP, QMessageBox.ButtonRole.RejectRole
        )
        self.button_open.clicked.connect(self._on_open)
        self.button_skip.clicked.connect(self._on_skip)
        self.set_default_button_to_skip()

    @property
    def site(self) -> str:
        """Площадка, к которой относится диалог."""
        return self._site

    @property
    def url(self) -> str:
        """Адрес страницы с CAPTCHA."""
        return self._url

    def set_default_button_to_skip(self) -> None:
        """Делает «Пропустить сайт» кнопкой по умолчанию и по ``Esc`` (CD-07).

        Закрытие крестиком/``Esc`` вызывает ``reject()``, который тоже
        трактуется как «Пропустить сайт».
        """
        self.setDefaultButton(self.button_skip)
        self.setEscapeButton(self.button_skip)

    # ------------------------------------------------------------------
    # Решения пользователя (CD-04, CD-05, CD-07)
    # ------------------------------------------------------------------
    def _on_open(self) -> None:
        """«Открыть браузер»: сигнал с URL и ``accept()`` (CD-04)."""
        if self._decided:
            return
        self._decided = True
        self.open_browser_requested.emit(self._url)
        self.accept()

    def _on_skip(self) -> None:
        """«Пропустить сайт»: сигнал и ``reject()`` (CD-05)."""
        self.reject()

    def reject(self) -> None:
        """Любое отклонение (кнопка, крестик, ``Esc``) — «Пропустить сайт».

        Сигнал ``skip_site_requested`` испускается не более одного раза и
        только если пользователь ещё не выбрал «Открыть браузер».
        """
        if not self._decided:
            self._decided = True
            self.skip_site_requested.emit()
        super().reject()

    # ------------------------------------------------------------------
    # Позиционирование (CD-01, W-03)
    # ------------------------------------------------------------------
    def _center(self) -> None:
        """Центрирует диалог; позиция не сохраняется (W-03)."""
        if self._window_manager is not None:
            self._window_manager.center_error(self)
        else:
            WindowManager.center(self)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        """Центрирует диалог при показе и повторно после расчёта размера."""
        super().showEvent(event)
        self._center()
        QTimer.singleShot(0, self._center)
