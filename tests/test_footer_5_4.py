"""Тесты 5.4: срок устаревания базы 180 дней, футер с копирайтом, клик по футеру."""

from __future__ import annotations

import ast
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from common import constants, footer
from common.config import Config
from common.footer import CopyrightFooter

ROOT = Path(__file__).resolve().parent.parent
URL = "https://coscore.ru"


def text_for(component: str) -> str:
    return f"АвтоПрайс 1.0 {component}. Copyright (c) CoScore, 2026"


def pump(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.005)
    assert cond(), "условие не выполнено за отведённое время"


# ----------------------------------------------------------------------
# 1. db_max_age_days = 180
# ----------------------------------------------------------------------
def test_default_db_max_age_is_180():
    assert constants.DEFAULT_SETTINGS["client"]["db_max_age_days"] == 180


def test_config_without_file_gives_180(tmp_path):
    assert Config(tmp_path / "settings.json").load().get("client", "db_max_age_days") == 180


def test_settings_dialog_spin_defaults_to_180(qapp, tmp_path):
    from client.settings_dialog import SettingsDialog

    db = tmp_path / "aprice.db"
    db.write_bytes(b"x")
    config = Config(tmp_path / "settings.json").load()
    assert SettingsDialog(config, db_path=db).age_spin.value() == 180
    # ключа нет в файле вовсе — запасное значение диалога тоже 180
    config.data["client"].pop("db_max_age_days", None)
    assert SettingsDialog(config, db_path=db).age_spin.value() == 180


# ----------------------------------------------------------------------
# 2. Футер в окнах
# ----------------------------------------------------------------------
def check_footer(label, component: str) -> None:
    assert isinstance(label, CopyrightFooter)
    assert label.text() == text_for(component)
    assert "Copyright (c) CoScore" in label.text()
    assert "#888" in label.styleSheet()
    assert label.cursor() == Qt.CursorShape.PointingHandCursor  # «рука» при наведении
    assert label.alignment() == Qt.AlignmentFlag.AlignCenter


def test_client_window_has_footer(qapp):
    from client.gui import MainWindow

    check_footer(MainWindow().footer_label, "Client")


def test_settings_dialog_has_footer(qapp, tmp_path):
    from client.settings_dialog import SettingsDialog

    config = Config(tmp_path / "settings.json").load()
    check_footer(SettingsDialog(config, db_path=tmp_path / "x.db").footer_label, "Client")


def test_builder_window_has_footer(qapp, tmp_path, monkeypatch):
    from builder import gui
    from builder.database import Database

    monkeypatch.setattr(gui, "Database", lambda *a, **k: Database(db_path=tmp_path / "gui.db"))
    monkeypatch.setattr(constants, "CACHE_DIR", tmp_path / "cache")
    window = gui.BuilderWindow(Config(tmp_path / "settings.json").load(), logging.NullHandler())
    check_footer(window.footer_label, "Builder")


def test_server_progress_window_has_footer(qapp):
    from PyQt6.QtCore import QObject, pyqtSignal
    from server.progress_window import ProgressWindow

    class Tm(QObject):
        task_started = pyqtSignal(str)
        task_progress = pyqtSignal(str, dict)
        task_completed = pyqtSignal(str, int)
        task_stopped = pyqtSignal(str, int)
        task_failed = pyqtSignal(str, str)
        captcha_required = pyqtSignal(str, str, str)

        def get_status(self, task_id):
            return {"state": "running", "eta_seconds": None, "task_id": task_id}

        def page_totals(self, task_id):
            return None

    check_footer(ProgressWindow(Tm()).footer_label, "Server")


@pytest.mark.parametrize("component", ["Client", "Server", "Builder"])
def test_footer_text_per_component(qapp, component):
    assert CopyrightFooter(component).text() == text_for(component)
    assert footer.footer_text(component) == text_for(component)


# ----------------------------------------------------------------------
# 3. Клик по футеру: проверка coscore.ru
# ----------------------------------------------------------------------
class Net:
    """Подмена ``requests.head/get`` и ``webbrowser.open``; пишет вызовы."""

    def __init__(self, monkeypatch, head=200, get=200):
        self.opened: list[str] = []
        self.calls: list[tuple] = []
        self.threads: list[threading.Thread] = []
        self._head, self._get = head, get
        monkeypatch.setattr(footer.requests, "head", self._request("head", lambda: self._head))
        monkeypatch.setattr(footer.requests, "get", self._request("get", lambda: self._get))
        monkeypatch.setattr(footer.webbrowser, "open", self.opened.append)

    def _request(self, name, outcome):
        def call(url, **kwargs):
            self.calls.append((name, url, kwargs))
            self.threads.append(threading.current_thread())
            result = outcome()
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(status_code=result, close=lambda: None)
        return call


def click(label: CopyrightFooter, button=Qt.MouseButton.LeftButton) -> None:
    label.mousePressEvent(SimpleNamespace(button=lambda: button))
    assert label.wait()
    QApplication.processEvents()


def test_click_opens_site_when_head_returns_200(qapp, monkeypatch):
    net = Net(monkeypatch, head=200)
    label = CopyrightFooter("Client")
    click(label)
    pump(lambda: net.opened)
    assert net.opened == [URL]
    name, url, kwargs = net.calls[0]
    assert (name, url) == ("head", URL)
    assert kwargs["timeout"] == 2.0 and kwargs["allow_redirects"] is True
    assert [c[0] for c in net.calls] == ["head"]


def test_check_runs_outside_gui_thread(qapp, monkeypatch):
    net = Net(monkeypatch)
    click(CopyrightFooter("Client"))
    assert net.threads and all(t is not threading.main_thread() for t in net.threads)


@pytest.mark.parametrize("outcome", [
    404, 500, 301, 503,
    requests.Timeout("слишком долго"), requests.ConnectionError("нет сети"),
    requests.exceptions.SSLError("сертификат"),
])
def test_click_does_nothing_when_site_is_down(qapp, monkeypatch, outcome):
    net = Net(monkeypatch, head=outcome)
    label = CopyrightFooter("Client")
    click(label)
    pump(lambda: net.calls)
    QApplication.processEvents()
    assert net.opened == []  # тихо игнорируем: без окон и исключений


def test_head_405_falls_back_to_get_stream(qapp, monkeypatch):
    net = Net(monkeypatch, head=405, get=200)
    click(CopyrightFooter("Client"))
    pump(lambda: net.opened)
    assert net.opened == [URL]
    assert [c[0] for c in net.calls] == ["head", "get"]
    assert net.calls[1][2]["stream"] is True and net.calls[1][2]["timeout"] == 2.0


def test_head_405_and_get_404_does_nothing(qapp, monkeypatch):
    net = Net(monkeypatch, head=405, get=404)
    click(CopyrightFooter("Client"))
    pump(lambda: len(net.calls) == 2)
    QApplication.processEvents()
    assert net.opened == []


def test_right_click_does_nothing(qapp, monkeypatch):
    net = Net(monkeypatch)
    click(CopyrightFooter("Client"), Qt.MouseButton.RightButton)
    assert net.calls == [] and net.opened == []


def test_second_click_while_checking_is_ignored(qapp, monkeypatch):
    gate = threading.Event()
    seen: list[str] = []

    def slow_head(url, **kw):
        seen.append(url)
        gate.wait(5)
        return SimpleNamespace(status_code=404, close=lambda: None)

    monkeypatch.setattr(footer.requests, "head", slow_head)
    label = CopyrightFooter("Client")
    assert label.activate() is True
    assert label.activate() is False  # предыдущая проверка ещё идёт
    gate.set()
    assert label.wait()
    assert seen == [URL]


def test_click_on_real_window_footer(qapp, monkeypatch):
    from client.gui import MainWindow

    net = Net(monkeypatch)
    window = MainWindow()
    click(window.footer_label)
    pump(lambda: net.opened)
    assert net.opened == [URL]


# ----------------------------------------------------------------------
# 4. Независимость Builder от Client/Server
# ----------------------------------------------------------------------
def imported_top_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("package", ["client", "server", "common"])
def test_package_does_not_import_builder(package):
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / package).rglob("*.py")
                 if "builder" in imported_top_packages(p)]
    assert offenders == []
