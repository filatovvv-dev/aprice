"""Иконка APrice-Server в системном трее (S-02, S-09, S-10).

:class:`TrayIcon` показывает состояние сбора во всплывающей подсказке,
открывает окно прогресса по двойному щелчку и предоставляет контекстное
меню («Прогресс», «Остановить сбор», «Настройки», «Выход»).

Класс **не** вызывает ``show()`` в конструкторе и не завершает приложение:
показ иконки, обработку ``stop_requested`` / ``quit_requested`` и
подключение к IPC выполняет точка входа (3.6).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from PyQt6.QtCore import QObject, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from common import constants
from common.utils import format_eta

logger = logging.getLogger("aprice.server.tray")

#: Подсказка в режиме ожидания (TR-02, TR-05).
TOOLTIP_IDLE = "АвтоПрайс: ожидание заданий"
#: Подсказка после завершения задания (TR-04).
TOOLTIP_DONE = "АвтоПрайс: задание завершено"

#: Размер программно рисуемой иконки, px.
ICON_SIZE = 64


def make_icon() -> QIcon:
    """Рисует иконку программно: синий круг с белой буквой «А» (TR-01).

    Внешних файлов не требуется; иконка гарантированно не пустая.
    """
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#1f6feb"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, ICON_SIZE - 4, ICON_SIZE - 4)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(ICON_SIZE * 0.6))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(
            pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), "А"
        )
    finally:
        painter.end()
    return QIcon(pixmap)


def open_in_system_editor(path: Any) -> None:
    """Открывает файл в программе по умолчанию (TR-07 «Настройки»).

    На Windows — ``os.startfile``; на остальных ОС (тесты, CI) —
    ``QDesktopServices``.
    """
    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        startfile(str(path))
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class TrayIcon(QSystemTrayIcon):
    """Иконка в трее с подсказкой о ходе сбора и контекстным меню.

    Signals:
        show_progress_requested: двойной щелчок или пункт «Прогресс».
        stop_requested: пункт «Остановить сбор».
        quit_requested: пункт «Выход».
    """

    show_progress_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, task_manager: Any, parent: Optional[QObject] = None) -> None:
        """Создаёт иконку и подключает её к сигналам ``TaskManager``.

        Args:
            task_manager: менеджер заданий (``task_started``,
                ``task_progress``, ``task_completed``, ``task_stopped``,
                ``task_failed``, ``get_status``).
            parent: родительский QObject.
        """
        super().__init__(make_icon(), parent)
        self._task_manager = task_manager
        self._build_menu()
        self.setToolTip(TOOLTIP_IDLE)
        self.set_task_active(False)

        self.activated.connect(self._on_activated)
        task_manager.task_started.connect(self._on_started)
        task_manager.task_progress.connect(self._on_progress)
        task_manager.task_completed.connect(self._on_completed)
        task_manager.task_stopped.connect(self._on_stopped)
        task_manager.task_failed.connect(self._on_failed)

    # ------------------------------------------------------------------
    # Меню (TR-07, TR-08)
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        """Строит контекстное меню; ссылка на него хранится в объекте."""
        self._menu = QMenu()
        self.action_progress = QAction("Прогресс", self._menu)
        self.action_stop = QAction("Остановить сбор", self._menu)
        self.action_settings = QAction("Настройки", self._menu)
        self.action_quit = QAction("Выход", self._menu)

        # Сигнал -> сигнал: Qt сам отбрасывает лишний аргумент ``checked``.
        self.action_progress.triggered.connect(self.show_progress_requested)
        self.action_stop.triggered.connect(self.stop_requested)
        self.action_settings.triggered.connect(self._open_settings)
        self.action_quit.triggered.connect(self.quit_requested)

        self._menu.addAction(self.action_progress)
        self._menu.addAction(self.action_stop)
        self._menu.addAction(self.action_settings)
        self._menu.addSeparator()
        self._menu.addAction(self.action_quit)
        self.setContextMenu(self._menu)

    def set_task_active(self, active: bool) -> None:
        """Включает пункт «Остановить сбор» только при активном задании (TR-08).

        Публичный метод нужен и точке входа: после ``restore()`` задание
        может быть активным ещё до первого сигнала.
        """
        self.action_stop.setEnabled(bool(active))

    def _open_settings(self) -> None:
        """Открывает ``settings.json`` в системном редакторе."""
        try:
            open_in_system_editor(constants.SETTINGS_FILE)
        except OSError:
            logger.exception("Не удалось открыть %s", constants.SETTINGS_FILE)

    # ------------------------------------------------------------------
    # Реакция на сигналы
    # ------------------------------------------------------------------
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Двойной щелчок открывает окно прогресса (TR-06, S-10)."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_progress_requested.emit()

    def _on_started(self, task_id: str) -> None:
        """Задание началось: «Остановить сбор» становится доступным."""
        self.set_task_active(True)
        self.setToolTip("АвтоПрайс: сбор данных")

    def _on_progress(self, task_id: str, progress: dict) -> None:
        """Подсказка с оставшимся временем (TR-03, S-09)."""
        self.set_task_active(True)
        status = self._task_manager.get_status(task_id) or {}
        eta = status.get("eta_seconds")
        if eta is None:
            self.setToolTip("АвтоПрайс: сбор данных")
        else:
            self.setToolTip(f"АвтоПрайс: сбор данных, осталось {format_eta(eta)}")

    def _on_completed(self, task_id: str, total_ads: int) -> None:
        """Задание завершено: подсказка и всплывающее сообщение (TR-04)."""
        self.set_task_active(False)
        self.setToolTip(TOOLTIP_DONE)
        self.showMessage(
            "АвтоПрайс",
            f"Сбор завершён: собрано объявлений — {total_ads}.",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def _on_stopped(self, task_id: str, total_ads: int) -> None:
        """Сбор остановлен: возврат в режим ожидания (TR-05)."""
        self.set_task_active(False)
        self.setToolTip(TOOLTIP_IDLE)

    def _on_failed(self, task_id: str, reason: str) -> None:
        """Ошибка или отмена: возврат в режим ожидания (TR-05)."""
        self.set_task_active(False)
        self.setToolTip(TOOLTIP_IDLE)
