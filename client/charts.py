"""Графики распределения цен APrice-Client (подзадача 4.3).

* :func:`draw_charts` рисует на ``matplotlib.figure.Figure`` гистограмму (20 бинов)
  и box plot (медиана, квартили, «усы», выбросы). Функция **не зависит от Qt** —
  тестируется без GUI.
* :class:`ChartWidget` — виджет PyQt6 с ``FigureCanvasQTAgg``.

Холст берётся из ``matplotlib.backends.backend_qtagg`` (он работает и с PyQt6;
``backend_qt5agg`` из примера ТЗ с PyQt6 не подходит). Создание холста вынесено в
:func:`_make_canvas`, чтобы окно можно было тестировать без настоящих Qt-биндингов.
Цены ``<= 0``/не числа отбрасываются так же, как в :mod:`client.stats`.
"""

from __future__ import annotations

from typing import Any, Iterable

from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from client.stats import valid_prices

HISTOGRAM_BINS = 20
NO_DATA_TEXT = "Нет данных для графиков"
_MILLION = 1_000_000


def _millions(value: float, _pos: Any = None) -> str:
    """Подпись оси: ``5640000`` -> ``5.6``."""
    return f"{value / _MILLION:.1f}"


def draw_charts(figure: Figure, prices: Iterable[Any]) -> None:
    """Очищает ``figure`` и рисует гистограмму (слева) и box plot (справа)."""
    values = valid_prices(prices)
    figure.clear()
    if not values:
        axes = figure.add_subplot(1, 1, 1)
        axes.text(0.5, 0.5, NO_DATA_TEXT, ha="center", va="center", transform=axes.transAxes)
        axes.set_axis_off()
        return

    hist = figure.add_subplot(1, 2, 1)
    hist.hist(values, bins=HISTOGRAM_BINS, color="#4c78a8", edgecolor="white")
    hist.set_title("Распределение цен")
    hist.set_xlabel("Цена, млн ₽")
    hist.set_ylabel("Объявлений")
    hist.xaxis.set_major_formatter(FuncFormatter(_millions))

    box = figure.add_subplot(1, 2, 2)
    box.boxplot(values, widths=0.5, patch_artist=True,
                boxprops={"facecolor": "#9ecae9"}, medianprops={"color": "#d62728"})
    box.set_title("Box plot")
    box.set_ylabel("Цена, млн ₽")
    box.set_xticks([])
    box.yaxis.set_major_formatter(FuncFormatter(_millions))
    figure.tight_layout()


def _make_canvas(figure: Figure) -> QWidget:
    """Qt-холст для ``figure`` (``FigureCanvasQTAgg``); импорт отложен."""
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

    return FigureCanvasQTAgg(figure)


class ChartWidget(QWidget):
    """Гистограмма и box plot цен; :meth:`update_data` перерисовывает их."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.figure = Figure(figsize=(5, 3))
        self.canvas = _make_canvas(self.figure)
        layout = QVBoxLayout(self)
        layout.addWidget(self.canvas)
        self.update_data([])

    def update_data(self, prices: Iterable[Any]) -> None:
        """Очистить и перерисовать графики; пустые данные — надпись «Нет данных»."""
        draw_charts(self.figure, prices)
        draw = getattr(self.canvas, "draw_idle", None)
        if callable(draw):
            draw()
