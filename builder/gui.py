"""GUI APrice-Builder (PyQt6).

Минимальное окно утилиты формирования справочной базы:

* кнопка «Обновить базу» — запуск парсинга в ``QThread`` (GUI не блокируется);
* кнопка «Остановить» — корректная прерывание фонового потока;
* строка статуса — дата последнего обновления и счётчики БД;
* лог-панель (``QPlainTextEdit``, read-only) с живой записью через сигнал
  ``log_message`` (логгер → слот, потокобезопасно);
* кнопка «Сохранить лог»;
* прогресс-бар (определённый при известном числе шагов, иначе indeterminate).

Позиционирование окна — через :class:`common.window_manager.WindowManager`
(W-01..W-06). Диалоги ошибок центрируются без сохранения позиции (W-03).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from builder.database import Database
from builder.parser import CaptchaDetectedError, CatalogParser
from common import constants
from common.config import Config
from common.logger import QtLogEmitter
from common.window_manager import WindowManager

logger = logging.getLogger(__name__)


class ParseWorker(QThread):
    """Фоновый поток выполнения :class:`CatalogParser`.

    Signals:
        progress: ``(step, total, message)`` — шаг/итог для прогресс-бара.
        finished_ok: парсинг успешно завершён.
        failed: текст ошибки для диалога (W-03).
    """

    progress = pyqtSignal(int, int, str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self, db: Database, config: Config, parent: Optional[QWidget] = None
    ) -> None:
        """Создаёт воркер.

        Args:
            db: Открытое соединение с БД (используется только из потока).
            config: Конфигурация приложения.
            parent: Необязательный родительский виджет.
        """
        super().__init__(parent)
        self._db = db
        self._config = config
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Запрашивает досрочную остановку парсинга."""
        logger.info("Запрошена остановка парсинга")
        self._stop_event.set()

    def run(self) -> None:  # noqa: D102 — docstring наследуется от QThread
        # Лимит марок читается из settings.json (builder.limit_brands;
        # null = без лимита) — задача №2, п. 1.4. GUI-чекбокс синхронизирует
        # этот же ключ конфига перед запуском воркера.
        parser = CatalogParser(
            db=self._db,
            config=self._config,
            progress_cb=lambda step, total, msg: self.progress.emit(step, total, msg),
            stop_event=self._stop_event,
        )
        try:
            parser.run()
        except InterruptedError:
            logger.warning("Парсинг остановлен пользователем")
            self.failed.emit("Парсинг остановлен пользователем")
        except CaptchaDetectedError as exc:
            logger.error("Блокировка источника: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 — показываем ошибку в GUI
            logger.exception("Парсинг завершился с ошибкой")
            self.failed.emit(f"Ошибка парсинга: {exc}")
        else:
            self.finished_ok.emit()


class BuilderWindow(QMainWindow):
    """Главное окно APrice-Builder."""

    WINDOW_ID = "builder_main"

    # Сигнал доставки строк журнала из любого потока в GUI-поток.
    log_message = pyqtSignal(str)

    def __init__(self, config: Config, gui_log_handler: logging.Handler) -> None:
        """Инициализирует окно.

        Args:
            config: Загруженная конфигурация.
            gui_log_handler: Хэндлер из :func:`common.logger.setup_logging`,
                к которому подключается сигнал ``log_message``.
        """
        super().__init__()
        self._config = config
        # Компонентный INI позиций окон (задача №2, п. 2.1): позиции
        # Builder не пересекаются с будущими окнами Server/Client.
        self._window_manager = WindowManager(
            settings_filename=constants.BUILDER_WINDOW_SETTINGS_FILE
        )
        self._worker: Optional[ParseWorker] = None

        self.setWindowTitle(constants.BUILDER_TITLE)
        self.setMinimumSize(720, 520)
        self.resize(860, 600)

        self._build_ui()
        self._connect_logger(gui_log_handler)
        self._refresh_status()

        # W-01/W-02/W-05: центр при первом запуске, восстановление после.
        self._window_manager.apply(self, window_id=self.WINDOW_ID)

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """Создаёт виджеты основного экрана."""
        central = QWidget(self)
        layout = QVBoxLayout(central)

        buttons_row = QHBoxLayout()
        self.btn_update = QPushButton("Обновить базу", central)
        self.btn_stop = QPushButton("Остановить", central)
        self.btn_stop.setEnabled(False)
        self.btn_save_log = QPushButton("Сохранить лог", central)
        buttons_row.addWidget(self.btn_update)
        buttons_row.addWidget(self.btn_stop)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self.btn_save_log)
        layout.addLayout(buttons_row)

        # Ограничение объёма прогона (задача №2, п. 1.4): состояние
        # двусторонне синхронизировано с settings.json (builder.limit_brands).
        self.chk_limit_brands = QCheckBox(
            "Ограничить 5 марками (валидация)", central
        )
        self.chk_limit_brands.setToolTip(
            "Снимите галочку для полного обхода всех марок каталога. "
            "Значение сохраняется в config/settings.json (builder.limit_brands)."
        )
        saved_limit = self._config.get("builder", "limit_brands", 5)
        self.chk_limit_brands.setChecked(saved_limit is not None)
        self.chk_limit_brands.toggled.connect(self._on_limit_toggled)
        layout.addWidget(self.chk_limit_brands)

        self.progress_bar = QProgressBar(central)
        self.progress_bar.setRange(0, 0)  # indeterminate до первого шага
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        self.log_view = QPlainTextEdit(central)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        font = QFont("Consolas", 9)
        self.log_view.setFont(font)
        layout.addWidget(self.log_view, stretch=1)

        self.status_label = QLabel("", self)
        self.statusBar().addWidget(self.status_label)

        self.setCentralWidget(central)

        self.btn_update.clicked.connect(self._on_update_clicked)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        self.btn_save_log.clicked.connect(self._on_save_log_clicked)

    def _connect_logger(self, handler: logging.Handler) -> None:
        """Подключает GUI-хэндлер логгера к сигналу ``log_message``.

        Сигнал гарантированно доставляет сообщения в поток GUI даже при
        логировании из фонового потока парсера.
        """
        self.log_message.connect(self.log_view.appendPlainText)
        if isinstance(handler, QtLogEmitter):
            handler.connect(lambda text: self.log_message.emit(text))

    # ------------------------------------------------------------------
    # Слоты
    # ------------------------------------------------------------------
    def _on_update_clicked(self) -> None:
        """Запускает парсинг в фоновом потоке (критерий 3)."""
        if self._worker is not None and self._worker.isRunning():
            return
        self.log_view.appendPlainText("— Начало обновления базы —")
        self.btn_update.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setRange(0, 0)

        db = Database()
        db.init_schema()
        self._worker = ParseWorker(db, self._config, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(db.close)
        self._worker.start()

    def _on_stop_clicked(self) -> None:
        """Обрабатывает нажатие «Остановить» (критерий 6)."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self.btn_stop.setEnabled(False)

    def _on_limit_toggled(self, checked: bool) -> None:
        """Сохраняет выбор лимита марок в settings.json (задача №2, п. 1.4).

        Args:
            checked: ``True`` — ограничивать 5 марками (валидация),
                ``False`` — полный обход (``limit_brands = null``).
        """
        self._config.set("builder", "limit_brands", 5 if checked else None)
        self._config.save()
        logger.info(
            "Лимит марок: %s (сохранено в settings.json)",
            "5 (валидация)" if checked else "без ограничения",
        )

    def _on_progress(self, step: int, total: int, message: str) -> None:
        """Обновляет прогресс-бар по данным парсера."""
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(step)
            self.progress_bar.setFormat(f"%v/%m — {message}")
        else:
            self.progress_bar.setRange(0, 0)

    def _on_finished(self) -> None:
        """Штатное завершение парсинга."""
        self._reset_buttons()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self._refresh_status()
        logger.info("База успешно обновлена")

    def _on_failed(self, message: str) -> None:
        """Ошибка или остановка парсинга — показать диалог (W-03)."""
        self._reset_buttons()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self._show_error(message)
        self._refresh_status()

    def _on_save_log_clicked(self) -> None:
        """Сохраняет содержимое лог-панели в файл через диалог."""
        default_name = datetime.now().strftime("aprice_builder_log_%Y%m%d_%H%M%S.txt")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить лог",
            str(constants.LOGS_DIR / default_name),
            "Текстовые файлы (*.txt);;Все файлы (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                self.log_view.toPlainText(), encoding="utf-8"
            )
            logger.info("Лог сохранён: %s", path)
        except OSError as exc:
            self._show_error(f"Не удалось сохранить лог: {exc}")

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------
    def _reset_buttons(self) -> None:
        """Возвращает кнопки в состояние покоя."""
        self.btn_update.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _refresh_status(self) -> None:
        """Обновляет строку статуса: last_update + счётчики таблиц."""
        try:
            with Database() as db:
                db.init_schema()
                counts = db.counts()
                last_update = db.get_metadata("last_update") or "нет данных"
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось прочитать статус БД: %s", exc)
            return
        self.status_label.setText(
            f"Последнее обновление: {last_update} | "
            f"марки: {counts['manufacturers']} | "
            f"модели: {counts['models']} | "
            f"поколения: {counts['generations']} | "
            f"конфигурации: {counts['configurations']}"
        )

    def _show_error(self, text: str) -> None:
        """Показывает модальный диалог ошибки в центре экрана (W-03)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Ошибка — APrice Builder")
        layout = QVBoxLayout(dialog)
        label = QLabel(text, dialog)
        label.setWordWrap(True)
        layout.addWidget(label)
        close_btn = QPushButton("Закрыть", dialog)
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.adjustSize()
        self._window_manager.center_error(dialog)
        dialog.exec()
