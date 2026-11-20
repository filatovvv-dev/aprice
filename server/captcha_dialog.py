"""Диалог CAPTCHA APrice-Server (S-13, A-08, W-03; 5.7 — восстановление сбора).

:class:`CaptchaDialog` просит пользователя решить CAPTCHA и продолжить сбор:

1. «Открыть браузер» — открывает страницу площадки (диалог **остаётся** на экране);
2. «Я решил, продолжить» — Server через несколько секунд повторяет ту же страницу;
3. «Пропустить сайт» — площадка пропускается, сбор идёт дальше.

Решения (задокументированы по CD-01, CD-06; изменены в 5.7):

* **Модальность (CD-06).** Диалог **немодальный** (``setModal(False)``): сервер продолжает
  обрабатывать сигналы, а показ диалога не требует вложенного цикла событий.
* **Центрирование (CD-01, W-03).** Позиция всегда по центру и не сохраняется:
  центрирование выполняется в ``showEvent`` и повторно через ``QTimer.singleShot(0, …)``.
* **Закрытие (CD-07).** Крестик и ``Esc`` равнозначны «Пропустить сайт» — пользователь
  никогда не «застревает» на капче.
* **QDialog, а не QMessageBox (5.7).** Кнопка «Открыть браузер» не должна закрывать окно
  (иначе нечем нажать «Я решил»), а ``QMessageBox`` закрывается любой своей кнопкой.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from common.window_manager import WindowManager

TITLE = "Обнаружена CAPTCHA"
TEXT = (
    "На площадке {site} требуется CAPTCHA.\n"
    "Откройте браузер, решите её, а затем нажмите «Я решил, продолжить»."
)
BUTTON_OPEN = "Открыть браузер"
BUTTON_RESUME = "Я решил, продолжить"
BUTTON_SKIP = "Пропустить сайт"


class CaptchaDialog(QDialog):
    """Диалог «Обнаружена CAPTCHA»: «Открыть браузер» / «Я решил, продолжить» / «Пропустить сайт».

    Signals:
        open_browser_requested(url): «Открыть браузер» (диалог остаётся открытым).
        resume_requested: «Я решил, продолжить» (закрывает диалог).
        skip_site_requested: «Пропустить сайт» (в т.ч. крестик/``Esc``); не более одного раза.
    """

    open_browser_requested = pyqtSignal(str)
    resume_requested = pyqtSignal()
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
        self._decided = False  # решение «продолжить»/«пропустить» принято (один раз)

        self.setWindowTitle(TITLE)
        self.setModal(False)  # CD-06

        self._label = QLabel(TEXT.format(site=site))
        self._label.setWordWrap(True)
        self.button_open = QPushButton(BUTTON_OPEN)
        self.button_resume = QPushButton(BUTTON_RESUME)
        self.button_skip = QPushButton(BUTTON_SKIP)

        buttons = QHBoxLayout()
        for button in (self.button_open, self.button_resume, self.button_skip):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        layout.addLayout(buttons)

        self.button_open.clicked.connect(self._on_open)
        self.button_resume.clicked.connect(self._on_resume)
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

    def text(self) -> str:
        """Текст сообщения (совместимо с прежним ``QMessageBox.text``)."""
        return self._label.text()

    def defaultButton(self) -> QPushButton:  # noqa: N802 - Qt-совместимое имя
        """Кнопка по умолчанию («Пропустить сайт», CD-07)."""
        return self.button_skip

    def set_default_button_to_skip(self) -> None:
        """Делает «Пропустить сайт» кнопкой по умолчанию и по ``Esc`` (CD-07).

        Закрытие крестиком/``Esc`` вызывает ``reject()``, который тоже
        трактуется как «Пропустить сайт».
        """
        for button in (self.button_open, self.button_resume):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.button_skip.setAutoDefault(True)
        self.button_skip.setDefault(True)

    # ------------------------------------------------------------------
    # Решения пользователя (CD-04, CD-05, CD-07; 5.7)
    # ------------------------------------------------------------------
    def _on_open(self) -> None:
        """«Открыть браузер»: сигнал с URL; диалог остаётся открытым (5.7)."""
        self.open_browser_requested.emit(self._url)

    def _on_resume(self) -> None:
        """«Я решил, продолжить»: сигнал и закрытие диалога."""
        if self._decided:
            return
        self._decided = True
        self.resume_requested.emit()
        self.accept()

    def _on_skip(self) -> None:
        """«Пропустить сайт»: сигнал и ``reject()`` (CD-05)."""
        self.reject()

    def reject(self) -> None:
        """Любое отклонение (кнопка, крестик, ``Esc``) — «Пропустить сайт».

        Сигнал ``skip_site_requested`` испускается не более одного раза и
        только если пользователь ещё не выбрал «Я решил, продолжить».
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
