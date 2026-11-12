"""Тесты GUI APrice-Server (подзадача 3.5): трей, окно прогресса, диалог CAPTCHA.

Работают с реальным PyQt6 на платформе ``offscreen`` (Linux CI) и на
обычном дисплее Windows. Qt-сигналы **не мокаются**: ``TaskManager``
заменён лёгким фейком с настоящими ``pyqtSignal``, поэтому поведение
подписок проверяется по-настоящему и без зависимости от планировщика.

Позиции окон проверяются относительно ``availableGeometry()`` экрана окна.
Для изоляции от рабочих настроек каждый тест получает временный
``QSettings`` через ``WindowManager(settings=...)``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QObject, QPoint, QSettings, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QCloseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from common import constants  # noqa: E402
from common.window_manager import WindowManager  # noqa: E402
from server import tray as tray_module  # noqa: E402
from server.captcha_dialog import CaptchaDialog  # noqa: E402
from server.progress_window import ProgressWindow  # noqa: E402
from server.tray import TOOLTIP_DONE, TOOLTIP_IDLE, TrayIcon  # noqa: E402

CENTER_TOLERANCE_PX = 5  # допуск центрирования диалога (ТЗ 3.5, п. 6.10)
WINDOW_CENTER_TOLERANCE_PX = 10  # окно прогресса (рамка/раскладка до show)


# ----------------------------------------------------------------------
# Инфраструктура
# ----------------------------------------------------------------------
class FakeTaskManager(QObject):
    """Минимальный двойник ``TaskManager``: настоящие сигналы, готовый статус."""

    task_started = pyqtSignal(str)
    task_progress = pyqtSignal(str, dict)
    task_completed = pyqtSignal(str, int)
    task_stopped = pyqtSignal(str, int)
    task_failed = pyqtSignal(str, str)
    captcha_required = pyqtSignal(str, str, str)

    def __init__(self) -> None:
        super().__init__()
        self.status: dict = {"state": "running", "eta_seconds": None}
        self.totals: tuple[int, int] | None = None

    def get_status(self, task_id: str) -> dict:
        return dict(self.status, task_id=task_id)

    def page_totals(self, task_id: str):  # noqa: ANN201
        return self.totals


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Один ``QApplication`` на модуль (виджетам нужен именно он)."""
    app = QCoreApplication.instance()
    if app is None:
        app = QApplication([])
    if not isinstance(app, QApplication):
        pytest.skip("уже создан QCoreApplication без GUI — виджеты недоступны")
    return app


@pytest.fixture()
def tm(qapp) -> FakeTaskManager:
    return FakeTaskManager()


@pytest.fixture()
def wm(qapp, tmp_path) -> WindowManager:
    """WindowManager с временным INI (позиции не попадают в config/)."""
    return WindowManager(
        settings=QSettings(str(tmp_path / "windows.ini"), QSettings.Format.IniFormat)
    )


@pytest.fixture()
def tray(qapp, tm) -> TrayIcon:
    return TrayIcon(tm)


@pytest.fixture()
def window(qapp, tm, wm):
    w = ProgressWindow(tm, window_manager=wm)
    yield w
    w.hide()
    w.deleteLater()


class Spy:
    """Считает срабатывания сигнала и запоминает аргументы."""

    def __init__(self, signal) -> None:  # noqa: ANN001
        self.calls: list[tuple] = []
        signal.connect(lambda *args: self.calls.append(args))


def available_center(widget) -> QPoint:
    return widget.screen().availableGeometry().center()


def assert_centered(widget, tolerance: int) -> None:
    center = widget.frameGeometry().center()
    target = available_center(widget)
    assert abs(center.x() - target.x()) <= tolerance, (center, target)
    assert abs(center.y() - target.y()) <= tolerance, (center, target)


RUNNING_STATUS = {
    "state": "running",
    "current_site": "auto.ru",
    "progress": {"page": 3},
    "eta_seconds": 1800,
}


# ----------------------------------------------------------------------
# TrayIcon
# ----------------------------------------------------------------------
def test_tray_initial_tooltip_and_icon(tray):
    """TR-01/TR-02: иконка не пустая, подсказка — «ожидание заданий»."""
    assert tray.toolTip() == TOOLTIP_IDLE == "АвтоПрайс: ожидание заданий"
    assert not tray.icon().isNull()
    assert not tray.icon().pixmap(32, 32).isNull()


def test_tray_progress_tooltip_shows_eta(tray, tm):
    """TR-03: ETA 3600 с -> подсказка содержит «~1 ч» (S-09)."""
    tm.status = {"state": "running", "eta_seconds": 3600}
    tm.task_progress.emit("t1", {"site": "auto.ru", "page": 1, "ads_collected": 20})
    assert "~1 ч" in tray.toolTip()
    assert tray.toolTip().startswith("АвтоПрайс: сбор данных, осталось")

    tm.status = {"state": "running", "eta_seconds": 3700}
    tm.task_progress.emit("t1", {})
    assert tray.toolTip() == "АвтоПрайс: сбор данных, осталось ~1 ч 1 мин"

    tm.status = {"state": "running", "eta_seconds": 1800}
    tm.task_progress.emit("t1", {})
    assert tray.toolTip() == "АвтоПрайс: сбор данных, осталось ~30 мин"


def test_tray_double_click_requests_progress(tray):
    """TR-06: двойной щелчок -> show_progress_requested; одиночный — нет."""
    spy = Spy(tray.show_progress_requested)
    tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert spy.calls == []
    tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert len(spy.calls) == 1


def test_tray_stop_action_enabled_only_while_task_active(tray, tm):
    """TR-07/TR-08: «Остановить сбор» активна только при активном задании."""
    assert tray.action_stop.isEnabled() is False
    tm.task_started.emit("t1")
    assert tray.action_stop.isEnabled() is True
    tm.task_completed.emit("t1", 40)
    assert tray.action_stop.isEnabled() is False

    tm.task_started.emit("t2")
    assert tray.action_stop.isEnabled() is True
    tm.task_stopped.emit("t2", 10)
    assert tray.action_stop.isEnabled() is False

    tm.task_started.emit("t3")
    tm.task_failed.emit("t3", "boom")
    assert tray.action_stop.isEnabled() is False


def test_tray_menu_actions_emit_signals(tray, tm, monkeypatch):
    """TR-07: пункты меню шлют сигналы; «Настройки» открывает settings.json."""
    assert [a.text() for a in tray.contextMenu().actions() if a.text()] == [
        "Прогресс",
        "Остановить сбор",
        "Настройки",
        "Выход",
    ]
    progress, stop, quit_ = (
        Spy(tray.show_progress_requested),
        Spy(tray.stop_requested),
        Spy(tray.quit_requested),
    )
    tray.action_progress.trigger()
    tray.action_stop.trigger()  # отключена -> без эффекта
    assert (len(progress.calls), len(stop.calls)) == (1, 0)
    tm.task_started.emit("t1")
    tray.action_stop.trigger()
    tray.action_quit.trigger()
    assert (len(stop.calls), len(quit_.calls)) == (1, 1)

    opened: list = []
    monkeypatch.setattr(tray_module, "open_in_system_editor", opened.append)
    tray.action_settings.trigger()
    assert opened == [constants.SETTINGS_FILE]


def test_tray_completion_and_idle_tooltips(tray, tm, monkeypatch):
    """TR-04/TR-05: завершение -> подсказка и сообщение; остановка/ошибка -> ожидание."""
    messages: list[tuple] = []
    monkeypatch.setattr(tray, "showMessage", lambda *a: messages.append(a))

    tm.task_completed.emit("t1", 120)
    assert tray.toolTip() == TOOLTIP_DONE == "АвтоПрайс: задание завершено"
    assert len(messages) == 1 and "120" in messages[0][1]

    tm.task_stopped.emit("t2", 5)
    assert tray.toolTip() == TOOLTIP_IDLE
    tm.task_completed.emit("t3", 1)
    tm.task_failed.emit("t3", "cancelled")
    assert tray.toolTip() == TOOLTIP_IDLE


# ----------------------------------------------------------------------
# ProgressWindow
# ----------------------------------------------------------------------
def test_progress_window_first_open_centered_then_remembers_position(
    qapp, tm, wm
):
    """W-01/W-02/W-04: центр при первом открытии; позиция помнится после hide/show
    и после «перезапуска» (новый экземпляр с тем же хранилищем)."""
    window = ProgressWindow(tm, window_manager=wm)
    window.show()
    qapp.processEvents()
    assert_centered(window, WINDOW_CENTER_TOLERANCE_PX)

    window.move(100, 120)
    window.close()  # closeEvent: позиция сохраняется, окно скрывается
    assert window.isHidden()
    window.show()
    assert (window.x(), window.y()) == (100, 120)
    window.hide()

    restarted = ProgressWindow(tm, window_manager=wm)
    assert (restarted.x(), restarted.y()) == (100, 120)
    restarted.deleteLater()
    window.deleteLater()


def test_progress_window_update_from_status(window):
    """PW-02/PW-09: площадка, счётчик, ETA и прогресс-бар обновляются."""
    window.update_from_status(RUNNING_STATUS)
    assert "auto.ru" in window.label_site.text()
    assert "~30 мин" in window.label_eta.text()
    assert window.label_ads.text() == "Собрано объявлений: 0"
    assert window.button_stop.isEnabled()
    assert window.windowTitle() != "Ожидание заданий"
    # нет total_pages -> «бегущий» прогресс-бар (PW-08)
    assert (window.progress_bar.minimum(), window.progress_bar.maximum()) == (0, 0)

    window.update_from_status(
        {
            **RUNNING_STATUS,
            "progress": {"page": 3, "total_pages": 10, "ads_collected": 60},
        }
    )
    assert (window.progress_bar.minimum(), window.progress_bar.maximum()) == (0, 10)
    assert window.progress_bar.value() == 3
    assert window.label_ads.text() == "Собрано объявлений: 60"


def test_progress_window_idle_state(window):
    """PW-03: без задания — «Ожидание заданий», кнопка отключена."""
    assert window.windowTitle() == "Ожидание заданий"
    assert window.button_stop.isEnabled() is False
    assert window.progress_bar.value() == 0
    window.update_from_status(RUNNING_STATUS)
    window.update_from_status({"state": "completed"})
    assert window.windowTitle() == "Ожидание заданий"
    assert window.button_stop.isEnabled() is False


def test_progress_window_follows_task_progress_signal(window, tm):
    """PW-04: task_progress обновляет окно; определённый бар — по page_totals."""
    tm.status = {
        "state": "running",
        "current_site": "drom.ru",
        "progress": {"site": "drom.ru", "page": 3, "ads_collected": 240},
        "eta_seconds": 3700,
    }
    tm.totals = (3, 12)
    tm.task_started.emit("t1")
    tm.task_progress.emit("t1", {"site": "drom.ru", "page": 3, "ads_collected": 240})
    assert "drom.ru" in window.label_site.text()
    assert window.label_ads.text() == "Собрано объявлений: 240"
    assert window.label_eta.text() == "Осталось: ~1 ч 1 мин"
    assert (window.progress_bar.maximum(), window.progress_bar.value()) == (12, 3)
    assert window.current_task_id == "t1"


@pytest.mark.parametrize("signal_name,args", [
    ("task_completed", ("t1", 40)),
    ("task_stopped", ("t1", 40)),
    ("task_failed", ("t1", "cancelled")),
])
def test_progress_window_hides_when_task_finished(window, tm, signal_name, args):
    """PW-05: completed/stopped/failed -> режим ожидания и скрытие окна."""
    window.show()
    tm.task_started.emit("t1")
    window.update_from_status(RUNNING_STATUS)
    assert window.isVisible()

    getattr(tm, signal_name).emit(*args)
    assert not window.isVisible()
    assert window.windowTitle() == "Ожидание заданий"
    assert window.button_stop.isEnabled() is False
    assert window.current_task_id is None


def test_progress_window_ignores_other_task_finish(window, tm):
    """Завершение постороннего задания не скрывает окно текущего."""
    window.show()
    tm.task_started.emit("t1")
    tm.task_completed.emit("other", 1)
    assert window.isVisible()


def test_progress_window_stop_button_emits_signal(window):
    """PW-06: «Остановить сбор» -> stop_requested (disabled -> без эффекта)."""
    spy = Spy(window.stop_requested)
    window.button_stop.click()  # отключена в режиме ожидания
    assert spy.calls == []
    window.update_from_status(RUNNING_STATUS)
    window.button_stop.click()
    assert len(spy.calls) == 1


def test_progress_window_close_only_hides(window, qapp, monkeypatch):
    """PW-07: close()/closeEvent скрывают окно, не уничтожают и не завершают приложение."""
    quits: list[int] = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(1)))
    window.show()
    assert window.isVisible()

    window.close()
    assert window.isHidden() and not sip.isdeleted(window)
    assert quits == []
    window.show()
    assert window.isVisible()

    event = QCloseEvent()
    window.closeEvent(event)  # как при Alt+F4
    assert event.isAccepted() is False
    assert window.isHidden() and not sip.isdeleted(window)
    assert quits == []


# ----------------------------------------------------------------------
# CaptchaDialog
# ----------------------------------------------------------------------
@pytest.fixture()
def dialog(qapp, wm):
    d = CaptchaDialog("auto.ru", "https://auto.ru/captcha?x=1", window_manager=wm)
    yield d
    d.hide()
    d.deleteLater()


def test_captcha_dialog_texts_and_buttons(dialog):
    """CD-02/CD-03/CD-06: тексты, кнопки и немодальность."""
    assert dialog.windowTitle() == "Обнаружена CAPTCHA"
    assert dialog.text() == (
        "На площадке auto.ru требуется прохождение CAPTCHA.\n"
        "Открыть страницу в браузере?"
    )
    assert dialog.button_open.text() == "Открыть браузер"
    assert dialog.button_skip.text() == "Пропустить сайт"
    assert dialog.isModal() is False
    assert dialog.defaultButton() is dialog.button_skip


def test_captcha_dialog_always_centered(dialog, qapp):
    """CD-01/W-03: при show() — по центру; позиция не запоминается."""
    dialog.show()
    qapp.processEvents()  # отложенное повторное центрирование
    assert_centered(dialog, CENTER_TOLERANCE_PX)

    dialog.move(10, 10)
    dialog.hide()
    dialog.show()
    qapp.processEvents()
    assert_centered(dialog, CENTER_TOLERANCE_PX)


def test_captcha_dialog_open_browser_emits_url(dialog):
    """CD-04: «Открыть браузер» -> open_browser_requested(url) + accept()."""
    opened, skipped = Spy(dialog.open_browser_requested), Spy(dialog.skip_site_requested)
    dialog.show()
    dialog.button_open.click()
    assert opened.calls == [("https://auto.ru/captcha?x=1",)]
    assert skipped.calls == []
    assert not dialog.isVisible()
    dialog.reject()  # после выбора «Открыть» пропуска быть не должно
    assert skipped.calls == []


def test_captcha_dialog_skip_site_emits_signal(dialog):
    """CD-05: «Пропустить сайт» -> skip_site_requested ровно один раз."""
    opened, skipped = Spy(dialog.open_browser_requested), Spy(dialog.skip_site_requested)
    dialog.show()
    dialog.button_skip.click()
    assert len(skipped.calls) == 1 and opened.calls == []
    assert not dialog.isVisible()
    dialog.reject()
    assert len(skipped.calls) == 1  # повторного сигнала нет


def test_captcha_dialog_reject_counts_as_skip(qapp, wm):
    """CD-07: закрытие крестиком/Esc (reject) = «Пропустить сайт»."""
    d = CaptchaDialog("drom.ru", "https://drom.ru/c", window_manager=wm)
    skipped = Spy(d.skip_site_requested)
    d.show()
    d.reject()
    assert len(skipped.calls) == 1
    d.deleteLater()


def test_show_current_refreshes_running_task_and_shows(window, tm):
    """PW-09 (3.6): show_current() при идущем задании сразу заполняет поля."""
    tm.current_task_id = lambda: "t1"  # type: ignore[method-assign]
    tm.status = dict(RUNNING_STATUS)
    window.show_current()
    assert window.isVisible()
    assert window.current_task_id == "t1"
    assert "auto.ru" in window.label_site.text()


def test_show_current_without_task_shows_idle(window, tm):
    tm.current_task_id = lambda: None  # type: ignore[method-assign]
    window.show_current()
    assert window.isVisible() and window.current_task_id is None
