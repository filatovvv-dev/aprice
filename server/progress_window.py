"""Окно прогресса APrice-Server (S-10, S-11, W-01..W-06).

:class:`ProgressWindow` показывает текущую площадку, число собранных
объявлений, прогресс-бар, оставшееся время и кнопку «Остановить сбор».
Окно открывает пользователь (двойной щелчок по трею) либо клиент; само оно
не показывается при поступлении прогресса.

Закрытие окна (крестик, ``Alt+F4``) только **скрывает** его: сервер
продолжает работу в трее (PW-07). Завершение приложения — забота точки
входа (3.6); при последнем скрытом окне приложению нужен
``setQuitOnLastWindowClosed(False)``.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from common import constants
from common.utils import format_eta
from common.window_manager import WindowManager

#: Идентификатор окна в INI позиций (PW-01, W-04).
WINDOW_ID = "server_progress"

TITLE_IDLE = "Ожидание заданий"
TITLE_ACTIVE = "АвтоПрайс: сбор данных"

#: Состояния задания, при которых окно показывает «активный» режим.
ACTIVE_STATES = ("running", "stopping")


class ProgressWindow(QWidget):
    """Окно прогресса сбора с кнопкой «Остановить сбор».

    Signals:
        stop_requested: нажата кнопка «Остановить сбор» (PW-06); номер
            задания см. :attr:`current_task_id`.
    """

    stop_requested = pyqtSignal()

    def __init__(
        self,
        task_manager: Any,
        parent: Optional[QWidget] = None,
        window_manager: Optional[WindowManager] = None,
    ) -> None:
        """Создаёт окно и подключает его к сигналам ``TaskManager``.

        Args:
            task_manager: менеджер заданий (``task_started``,
                ``task_progress``, ``task_completed``, ``task_stopped``,
                ``task_failed``, ``get_status``; необязательный
                ``page_totals`` — для определённого прогресс-бара).
            parent: родительский виджет.
            window_manager: менеджер позиций окон. По умолчанию создаётся
                ``WindowManager(settings_filename=SERVER_WINDOW_SETTINGS_FILE)``;
                параметр нужен тестам (изолированный ``QSettings``).
        """
        super().__init__(parent)
        self._task_manager = task_manager
        self._task_id: Optional[str] = None
        self._window_manager = window_manager or WindowManager(
            settings_filename=constants.SERVER_WINDOW_SETTINGS_FILE
        )

        self._build_ui()
        self.set_idle()

        task_manager.task_started.connect(self._on_started)
        task_manager.task_progress.connect(self._on_progress)
        task_manager.task_completed.connect(self._on_finished)
        task_manager.task_stopped.connect(self._on_finished)
        task_manager.task_failed.connect(self._on_finished)

        # W-01/W-02/W-05: центр при первом открытии, восстановление после.
        self.adjustSize()
        self._window_manager.apply(self, WINDOW_ID)

    # ------------------------------------------------------------------
    # Интерфейс (PW-02)
    # ------------------------------------------------------------------
    @property
    def current_task_id(self) -> Optional[str]:
        """Идентификатор отображаемого задания или ``None`` в режиме ожидания."""
        return self._task_id

    def _build_ui(self) -> None:
        """Создаёт виджеты окна."""
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        self.label_site = QLabel(self)
        self.label_ads = QLabel(self)
        self.progress_bar = QProgressBar(self)
        self.label_eta = QLabel(self)
        self.button_stop = QPushButton("Остановить сбор", self)
        for widget in (
            self.label_site,
            self.label_ads,
            self.progress_bar,
            self.label_eta,
            self.button_stop,
        ):
            layout.addWidget(widget)
        self.button_stop.clicked.connect(self._on_stop_clicked)

    def _on_stop_clicked(self) -> None:
        """Кнопка «Остановить сбор» (PW-06)."""
        self.stop_requested.emit()

    # ------------------------------------------------------------------
    # Режимы
    # ------------------------------------------------------------------
    def set_idle(self) -> None:
        """Режим «ожидание»: нет задания, кнопка отключена (PW-03).

        Прогресс-бар показывает ``0`` (а не «бегущую» неопределённость,
        чтобы не создавать впечатление работы).
        """
        self._task_id = None
        self.setWindowTitle(TITLE_IDLE)
        self.label_site.setText("Текущая площадка: —")
        self.label_ads.setText("Собрано объявлений: 0")
        self.label_eta.setText("Осталось: —")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.button_stop.setEnabled(False)

    def update_from_status(self, status: dict) -> None:
        """Обновляет все поля из ``StatusMsg``-совместимого словаря (PW-09).

        Используется и при поступлении прогресса, и при открытии окна во
        время уже идущего сбора. Состояние вне ``running``/``stopping``
        переключает окно в режим ожидания.

        Прогресс-бар определённый (PW-08), если в ``progress`` есть
        ``total_pages`` и ``pages_done`` либо ``page``; иначе — «бегущий»
        (``setRange(0, 0)``).

        Args:
            status: словарь вида ``{"state", "current_site", "progress",
                "eta_seconds", ...}``.
        """
        if status.get("state") not in ACTIVE_STATES:
            self.set_idle()
            return

        progress = status.get("progress") or {}
        self.setWindowTitle(TITLE_ACTIVE)
        self.label_site.setText(
            f"Текущая площадка: {status.get('current_site') or '—'}"
        )
        self.label_ads.setText(
            f"Собрано объявлений: {int(progress.get('ads_collected') or 0)}"
        )
        eta = status.get("eta_seconds")
        self.label_eta.setText(
            "Осталось: —" if eta is None else f"Осталось: {format_eta(eta)}"
        )

        total = progress.get("total_pages")
        current = progress.get("pages_done", progress.get("page"))
        if isinstance(total, int) and total > 0 and isinstance(current, int):
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(max(0, min(current, total)))
        else:
            self.progress_bar.setRange(0, 0)

        # «Остановка…» — повторное нажатие не нужно.
        self.button_stop.setEnabled(status.get("state") == "running")

    # ------------------------------------------------------------------
    # Реакция на сигналы TaskManager (PW-04, PW-05)
    # ------------------------------------------------------------------
    def _on_started(self, task_id: str) -> None:
        """Новое задание: запоминаем номер, окно остаётся скрытым."""
        self._task_id = task_id
        self.button_stop.setEnabled(True)

    def _on_progress(self, task_id: str, progress: dict) -> None:
        """Обновляет окно по ``task_progress`` (PW-04)."""
        self._task_id = task_id
        status = dict(self._task_manager.get_status(task_id) or {})
        status.setdefault("state", "running")
        merged = dict(status.get("progress") or progress)
        totals_fn = getattr(self._task_manager, "page_totals", None)
        totals = totals_fn(task_id) if callable(totals_fn) else None
        if totals is not None:
            merged["pages_done"], merged["total_pages"] = totals
        status["progress"] = merged
        self.update_from_status(status)

    def _on_finished(self, task_id: str, *_: Any) -> None:
        """Задание завершено/остановлено/упало: ожидание и скрытие (PW-05).

        Скрытие идёт через ``close()``: ``closeEvent`` игнорирует закрытие и
        вызывает ``hide()``, а ``WindowManager`` по пути сохраняет позицию
        окна (W-02/W-04) — голый ``hide()`` её бы не сохранил.
        """
        if self._task_id not in (None, task_id):
            return  # сигнал о другом (не отображаемом) задании
        self.set_idle()
        self.close()

    def show_current(self) -> None:
        """Обновляет окно по идущему заданию и показывает его (PW-09).

        Вызывается точкой входа по двойному щелчку на трее и пункту меню
        «Прогресс»: если сбор уже идёт, поля сразу актуальны (а не ждут
        следующего ``task_progress``); без задания окно открывается в режиме
        ожидания. Номер задания берётся у менеджера (``current_task_id``),
        при его отсутствии — из последнего ``task_started``.
        """
        getter = getattr(self._task_manager, "current_task_id", None)
        task_id = getter() if callable(getter) else self._task_id
        if task_id is not None:
            self._on_progress(task_id, {})
        else:
            self.set_idle()
        self.show()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------
    # Закрытие (PW-07)
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Крестик/Alt+F4 только скрывают окно, не уничтожая его и не
        завершая приложение."""
        event.ignore()
        self.hide()
