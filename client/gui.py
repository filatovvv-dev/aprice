"""Главное окно APrice-Client (подзадача 4.1): форма, прогресс, результаты.

Окно — «тонкое представление»: логику (справочники, отправка, опрос) ведёт
:class:`client.controller.ClientController`, а проверка готовности формы и
сборка ``spec`` — :mod:`client.form` (без Qt). Окно:

* читает виджеты в :class:`client.form.FormState` (:meth:`MainWindow.form_state`);
* заполняет списки по командам контроллера (``set_*``);
* сообщает о действиях пользователя сигналами (``evaluate_requested``,
  ``stop_requested``, ``manufacturer_changed``, ``model_changed``,
  ``generation_changed``).

Программное заполнение списков идёт при заблокированных сигналах: смена
выбора вызывает только пользователь.

Дизайн минималистичный; в таблице результатов пока только количество объявлений,
строки статистики (min/max/mean/median/trimmed mean) заполнит подзадача 4.2.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client import form
from client.form import FormState

WINDOW_TITLE = "АвтоПрайс — оценка стоимости автомобиля"
PLACEHOLDER = "— выберите —"
ANY_TEXT = "Любая"
#: Строки таблицы результатов; значения посчитает подзадача 4.2.
STAT_ROWS = ("Минимум", "Максимум", "Среднее", "Медиана", "Усечённое среднее")
NO_VALUE = "—"


class MainWindow(QMainWindow):
    """Главное окно Client."""

    evaluate_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    manufacturer_changed = pyqtSignal(str)
    model_changed = pyqtSignal(str)
    generation_changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self._running = False

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.addWidget(self._build_mode_panel())
        layout.addWidget(self._build_common_panel())
        layout.addWidget(self._build_exact_panel())
        layout.addWidget(self._build_range_panel())
        layout.addWidget(self._build_action_panel())
        layout.addWidget(self._build_progress_panel())
        layout.addWidget(self._build_results_panel())

        self._connect_signals()
        self._apply_mode()
        self._update_buttons()

    # ------------------------------------------------------------------
    # Построение
    # ------------------------------------------------------------------
    def _build_mode_panel(self) -> QWidget:
        box = QGroupBox("Режим")
        row = QHBoxLayout(box)
        self.radio_exact = QRadioButton("Точный автомобиль")
        self.radio_range = QRadioButton("Прицениться")
        self.radio_exact.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.radio_exact)
        self._mode_group.addButton(self.radio_range)
        row.addWidget(self.radio_exact)
        row.addWidget(self.radio_range)
        return box

    def _build_common_panel(self) -> QWidget:
        box = QGroupBox("Автомобиль")
        row = QHBoxLayout(box)
        self.manufacturer_combo = QComboBox()
        self.model_combo = QComboBox()
        for caption, combo in (("Марка", self.manufacturer_combo), ("Модель", self.model_combo)):
            row.addWidget(QLabel(caption))
            row.addWidget(combo)
            self._reset_combo(combo, [], PLACEHOLDER)
        return box

    def _build_exact_panel(self) -> QGroupBox:
        self.exact_box = QGroupBox("Точный автомобиль")
        row = QHBoxLayout(self.exact_box)
        self.generation_combo = QComboBox()
        self.year_combo = QComboBox()
        self.transmission_combo = QComboBox()
        self.fuel_combo = QComboBox()
        for caption, combo in (
            ("Поколение", self.generation_combo),
            ("Год", self.year_combo),
            ("КПП", self.transmission_combo),
            ("Топливо", self.fuel_combo),
        ):
            row.addWidget(QLabel(caption))
            row.addWidget(combo)
        self._reset_combo(self.generation_combo, [], ANY_TEXT)
        self._reset_combo(self.transmission_combo, list(form.TRANSMISSIONS), ANY_TEXT)
        self._reset_combo(self.fuel_combo, list(form.FUELS), ANY_TEXT)
        return self.exact_box

    def _build_range_panel(self) -> QGroupBox:
        self.range_box = QGroupBox("Прицениться")
        outer = QVBoxLayout(self.range_box)

        years = QHBoxLayout()
        self.year_from_spin = QSpinBox()
        self.year_to_spin = QSpinBox()
        for spin in (self.year_from_spin, self.year_to_spin):
            spin.setRange(form.MIN_YEAR, form.max_year())
        self.year_from_spin.setValue(form.max_year() - 8)
        self.year_to_spin.setValue(form.max_year())
        for caption, widget in (("Год от", self.year_from_spin), ("до", self.year_to_spin)):
            years.addWidget(QLabel(caption))
            years.addWidget(widget)

        volume = QHBoxLayout()
        self.volume_from_spin = QDoubleSpinBox()
        self.volume_to_spin = QDoubleSpinBox()
        for spin in (self.volume_from_spin, self.volume_to_spin):
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            spin.setRange(0.0, 10.0)
            spin.setSpecialValueText(NO_VALUE)  # 0 — граница не задана
            spin.setValue(0.0)
        for caption, widget in (("Объём от", self.volume_from_spin), ("до", self.volume_to_spin)):
            volume.addWidget(QLabel(caption))
            volume.addWidget(widget)

        self.transmission_checks = {name: QCheckBox(name) for name in form.TRANSMISSIONS}
        self.fuel_checks = {name: QCheckBox(name) for name in form.FUELS}
        trans_row, fuel_row = QHBoxLayout(), QHBoxLayout()
        trans_row.addWidget(QLabel("КПП"))
        for check in self.transmission_checks.values():
            trans_row.addWidget(check)
        fuel_row.addWidget(QLabel("Топливо"))
        for check in self.fuel_checks.values():
            fuel_row.addWidget(check)

        for row in (years, volume, trans_row, fuel_row):
            outer.addLayout(row)
        return self.range_box

    def _build_action_panel(self) -> QWidget:
        panel = QWidget()
        row = QHBoxLayout(panel)
        self.evaluate_button = QPushButton("Оценить")
        self.stop_button = QPushButton("Остановить")
        row.addWidget(self.evaluate_button)
        row.addWidget(self.stop_button)
        return panel

    def _build_progress_panel(self) -> QWidget:
        box = QGroupBox("Прогресс")
        layout = QVBoxLayout(box)
        self.status_label = QLabel("Готов к работе")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        return box

    def _build_results_panel(self) -> QWidget:
        box = QGroupBox("Результат")
        layout = QVBoxLayout(box)
        self.count_label = QLabel("Объявлений: —")
        self.stats_table = QTableWidget()
        self.stats_table.setColumnCount(2)
        self.stats_table.setHorizontalHeaderLabels(["Показатель", "Значение"])
        self.stats_table.setRowCount(len(STAT_ROWS))
        self.sites_table = QTableWidget()
        self.sites_table.setColumnCount(2)
        self.sites_table.setHorizontalHeaderLabels(["Площадка", "Объявлений"])
        layout.addWidget(self.count_label)
        layout.addWidget(self.stats_table)
        layout.addWidget(self.sites_table)
        self.clear_results()
        return box

    def _connect_signals(self) -> None:
        self.radio_exact.toggled.connect(self._on_mode_toggled)
        self.manufacturer_combo.currentIndexChanged.connect(
            lambda _i: self.manufacturer_changed.emit(self._selected(self.manufacturer_combo))
        )
        self.model_combo.currentIndexChanged.connect(
            lambda _i: self.model_changed.emit(self._selected(self.model_combo))
        )
        self.generation_combo.currentIndexChanged.connect(
            lambda _i: self.generation_changed.emit(self._selected(self.generation_combo))
        )
        for combo in (self.manufacturer_combo, self.model_combo, self.year_combo):
            combo.currentIndexChanged.connect(lambda _i: self._update_buttons())
        for spin in (self.year_from_spin, self.year_to_spin):
            spin.valueChanged.connect(lambda _v: self._update_buttons())
        for spin in (self.volume_from_spin, self.volume_to_spin):
            spin.valueChanged.connect(lambda _v: self._update_buttons())
        self.evaluate_button.clicked.connect(lambda _checked=False: self.evaluate_requested.emit())
        self.stop_button.clicked.connect(lambda _checked=False: self.stop_requested.emit())

    # ------------------------------------------------------------------
    # Режимы и состояние формы
    # ------------------------------------------------------------------
    def _on_mode_toggled(self, _checked: bool) -> None:
        self._apply_mode()
        self._update_buttons()

    def _apply_mode(self) -> None:
        """Режим А — комбобоксы, режим Б — диапазоны и чекбоксы."""
        exact = self.radio_exact.isChecked()
        self.exact_box.setVisible(exact)
        self.range_box.setVisible(not exact)

    def form_state(self) -> FormState:
        """Текущие значения формы."""
        exact = self.radio_exact.isChecked()
        year_text = self.year_combo.currentText()
        volume_from = self.volume_from_spin.value()
        volume_to = self.volume_to_spin.value()
        return FormState(
            mode=form.MODE_EXACT if exact else form.MODE_RANGE,
            manufacturer=self._selected(self.manufacturer_combo),
            model=self._selected(self.model_combo),
            generation=self._selected(self.generation_combo),
            year=int(year_text) if year_text.isdigit() else None,
            transmission=self._selected(self.transmission_combo),
            fuel=self._selected(self.fuel_combo),
            year_from=self.year_from_spin.value(),
            year_to=self.year_to_spin.value(),
            volume_from=volume_from if volume_from > 0 else None,
            volume_to=volume_to if volume_to > 0 else None,
            transmissions=[n for n, c in self.transmission_checks.items() if c.isChecked()],
            fuels=[n for n, c in self.fuel_checks.items() if c.isChecked()],
        )

    def _update_buttons(self) -> None:
        """«Оценить» активна, когда форма готова и сбор не идёт; «Остановить» — наоборот."""
        self.evaluate_button.setEnabled(not self._running and form.is_ready(self.form_state()))
        self.stop_button.setEnabled(self._running)

    # ------------------------------------------------------------------
    # Команды контроллера
    # ------------------------------------------------------------------
    def set_manufacturers(self, names: list[str]) -> None:
        self._reset_combo(self.manufacturer_combo, names, PLACEHOLDER)
        self._update_buttons()

    def set_models(self, names: list[str]) -> None:
        self._reset_combo(self.model_combo, names, PLACEHOLDER)
        self._update_buttons()

    def set_generations(self, names: list[str]) -> None:
        self._reset_combo(self.generation_combo, names, ANY_TEXT)

    def set_transmissions(self, names: list[str]) -> None:
        self._reset_combo(self.transmission_combo, names, ANY_TEXT)

    def set_fuels(self, names: list[str]) -> None:
        self._reset_combo(self.fuel_combo, names, ANY_TEXT)

    def set_years(self, years: list[int]) -> None:
        """Заполняет годы режима А; выбран первый (самый новый)."""
        self._reset_combo(self.year_combo, [str(y) for y in years], None)
        self._update_buttons()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def set_running(self, running: bool) -> None:
        """Идёт сбор: индикатор «занят», «Оценить» выключена, «Остановить» включена."""
        self._running = running
        if running:
            self.progress_bar.setRange(0, 0)  # indeterminate
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
        self._update_buttons()

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "АвтоПрайс", message)

    def clear_results(self) -> None:
        self.count_label.setText("Объявлений: —")
        for row, name in enumerate(STAT_ROWS):
            self.stats_table.setItem(row, 0, QTableWidgetItem(name))
            self.stats_table.setItem(row, 1, QTableWidgetItem(NO_VALUE))
        self.sites_table.setRowCount(0)

    def show_results(self, total: int, by_site: dict[str, int]) -> None:
        """Количество объявлений и разбивка по площадкам (статистика — 4.2)."""
        self.count_label.setText(f"Объявлений: {total}")
        self.sites_table.setRowCount(len(by_site))
        for row, (site, count) in enumerate(by_site.items()):
            self.sites_table.setItem(row, 0, QTableWidgetItem(site))
            self.sites_table.setItem(row, 1, QTableWidgetItem(str(count)))

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    @staticmethod
    def _selected(combo: QComboBox) -> str:
        """Выбранный текст или ``""`` для строки-заполнителя (индекс 0 у «необязательных»)."""
        if combo.currentIndex() <= 0 and combo.itemText(0) in (PLACEHOLDER, ANY_TEXT):
            return ""
        return combo.currentText()

    @staticmethod
    def _reset_combo(combo: QComboBox, items: list[str], first: Optional[str]) -> None:
        """Перезаполняет список без сигналов; ``first`` — строка «не выбрано» или ``None``."""
        combo.blockSignals(True)
        try:
            combo.clear()
            if first is not None:
                combo.addItem(first)
            for item in items:
                combo.addItem(item)
            combo.setCurrentIndex(0 if combo.count() else -1)
        finally:
            combo.blockSignals(False)
