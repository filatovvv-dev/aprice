"""Тесты client.charts (4.3): рисование без Qt и ChartWidget."""

import pytest
from matplotlib.figure import Figure

from client.charts import HISTOGRAM_BINS, NO_DATA_TEXT, ChartWidget, draw_charts


def titles(figure):
    return [ax.get_title() for ax in figure.axes]


def test_draw_creates_histogram_and_boxplot_axes():
    figure = Figure(figsize=(5, 3))
    draw_charts(figure, [100, 200, 300])
    assert titles(figure) == ["Распределение цен", "Box plot"]
    hist, box = figure.axes
    assert sum(p.get_height() for p in hist.patches) == 3  # все три цены в гистограмме
    assert len(hist.patches) == HISTOGRAM_BINS == 20
    assert hist.get_xlabel() == "Цена, млн ₽" and hist.get_ylabel() == "Объявлений"
    assert len(box.lines) > 0  # «ящик», усы, медиана нарисованы


def test_boxplot_median_is_correct():
    figure = Figure()
    draw_charts(figure, [1_000_000, 2_000_000, 3_000_000, 4_000_000, 100_000_000])
    box = figure.axes[1]
    medians = [line.get_ydata()[0] for line in box.lines if line.get_color() == "#d62728"]
    assert medians == [3_000_000]
    fliers = [list(line.get_ydata()) for line in box.lines if line.get_marker() == "o"]
    assert fliers == [[100_000_000]]  # выброс нарисован отдельной точкой


def test_empty_prices_show_no_data_message():
    figure = Figure()
    draw_charts(figure, [])
    assert len(figure.axes) == 1
    assert [t.get_text() for t in figure.axes[0].texts] == [NO_DATA_TEXT]


def test_invalid_prices_are_dropped_like_in_stats():
    figure = Figure()
    draw_charts(figure, [None, -5, 0, "x", float("nan"), 500, 700])
    hist = figure.axes[0]
    assert sum(p.get_height() for p in hist.patches) == 2


def test_redraw_replaces_previous_axes():
    figure = Figure()
    draw_charts(figure, [1, 2, 3])
    draw_charts(figure, [4, 5, 6])
    assert len(figure.axes) == 2
    draw_charts(figure, [])
    assert len(figure.axes) == 1


def test_single_price_does_not_crash():
    figure = Figure()
    draw_charts(figure, [5_000_000])
    assert len(figure.axes) == 2


def test_axis_labels_in_millions():
    figure = Figure()
    draw_charts(figure, [5_640_000, 6_000_000])
    formatter = figure.axes[0].xaxis.get_major_formatter()
    assert formatter(5_640_000) == "5.6"


def test_chart_widget_created(qapp):
    widget = ChartWidget()
    assert isinstance(widget.figure, Figure) and widget.canvas is not None
    assert [t.get_text() for t in widget.figure.axes[0].texts] == [NO_DATA_TEXT]


def test_chart_widget_update_data_creates_axes(qapp):
    widget = ChartWidget()
    widget.update_data([100, 200, 300])
    assert len(widget.figure.axes) == 2
    widget.update_data([])
    assert len(widget.figure.axes) == 1
