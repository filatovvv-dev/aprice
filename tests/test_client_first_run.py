"""Тесты 5.5: автоскачивание справочника при старте Client."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from PyQt6.QtWidgets import QApplication

from client import db_update, first_run
from client.first_run import FAILED_MESSAGE, NO_URL_MESSAGE, ensure_database
from client.settings_dialog import DbUpdateThread
from common.config import Config
from tests.test_db_update import FakeGet, FakeResponse, TextResponse, make_db

URL = "https://mirror.test/aprice.db"


class FakeUi:
    def __init__(self, outcome=(True, "")):
        self.outcome = outcome
        self.runs: list = []
        self.errors: list[str] = []

    def run(self, thread):
        self.runs.append(thread)
        return self.outcome

    def show_error(self, message):
        self.errors.append(message)


@pytest.fixture
def config(tmp_path):
    cfg = Config(tmp_path / "settings.json").load()
    cfg.set("client", "db_update_url", URL)
    return cfg


class RealThreadUi:
    """Запускает настоящий ``DbUpdateThread`` и ждёт его сигналы (без диалога)."""

    def __init__(self):
        self.errors: list[str] = []

    def run(self, thread):
        outcome: list = []
        thread.succeeded.connect(lambda r: outcome.append((True, "")))
        thread.failed.connect(lambda m: outcome.append((False, m)))
        thread.start()
        deadline = time.monotonic() + 5
        while not outcome and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        thread.wait()
        return outcome[0] if outcome else (False, "таймаут теста")

    def show_error(self, message):
        self.errors.append(message)


# ----------------------------------------------------------------------
# Логика ensure_database
# ----------------------------------------------------------------------
def test_existing_database_is_not_downloaded(config, tmp_path):
    db = tmp_path / "aprice.db"
    db.write_bytes(b"x")
    ui = FakeUi()
    assert ensure_database(config, db, ui, thread_factory=lambda *a: pytest.fail("не должен качать")) is True
    assert ui.runs == [] and ui.errors == []


def test_empty_file_counts_as_missing(config, tmp_path):
    db = tmp_path / "aprice.db"
    db.write_bytes(b"")
    ui = FakeUi((False, "нет сети"))
    assert ensure_database(config, db, ui, thread_factory=lambda u, t: "thread") is False
    assert ui.runs == ["thread"]


def test_missing_database_starts_download_from_config_url(config, tmp_path):
    db = tmp_path / "data" / "aprice.db"
    made: list = []

    def factory(url, target):
        made.append((url, target))
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"sqlite")  # «скачали»
        return "thread"

    ui = FakeUi((True, ""))
    assert ensure_database(config, db, ui, thread_factory=factory) is True
    assert made == [(URL, db)] and ui.runs == ["thread"] and ui.errors == []


def test_download_failure_shows_error_and_returns_false(config, tmp_path):
    ui = FakeUi((False, "Не удалось подключиться к зеркалу"))
    assert ensure_database(config, tmp_path / "aprice.db", ui, thread_factory=lambda u, t: "t") is False
    assert len(ui.errors) == 1
    assert ui.errors[0] == FAILED_MESSAGE.format(reason="Не удалось подключиться к зеркалу")
    assert "Проверьте подключение" in ui.errors[0]


def test_reported_success_without_file_is_failure(config, tmp_path):
    ui = FakeUi((True, ""))
    assert ensure_database(config, tmp_path / "aprice.db", ui, thread_factory=lambda u, t: "t") is False
    assert len(ui.errors) == 1


def test_empty_url_reports_problem_without_download(config, tmp_path):
    config.set("client", "db_update_url", "  ")
    config.set("client", "db_date_url", "")  # нет ни зеркала, ни манифеста
    ui = FakeUi()
    assert ensure_database(config, tmp_path / "aprice.db", ui, thread_factory=lambda *a: pytest.fail("нет")) is False
    assert ui.errors == [NO_URL_MESSAGE] and ui.runs == []


def test_progress_text_matches_requirement():
    assert first_run.PROGRESS_TEXT == "Загрузка справочника... Это займет около минуты"


# ----------------------------------------------------------------------
# С настоящим DbUpdateThread и подменённым requests
# ----------------------------------------------------------------------
DATE_URL = "https://mirror.test/date.txt"
MANIFEST_URL = "https://new.url/aprice.db"


def _count_manufacturers(db):
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM manufacturers").fetchone()[0]
    finally:
        conn.close()


def _first_run(config, tmp_path, monkeypatch, *responses):
    fake = FakeGet(*responses)
    monkeypatch.setattr(db_update.requests, "get", fake)
    config.set("client", "db_date_url", DATE_URL)
    db = tmp_path / "data" / "aprice.db"
    db.parent.mkdir()
    ui = RealThreadUi()
    return fake, db, ui, ensure_database(config, db, ui)


def test_first_run_uses_manifest_url(qapp, config, tmp_path, monkeypatch):
    payload = make_db(tmp_path / "remote.db", ("BMW", "Audi", "Chery"))
    fake, db, ui, ok = _first_run(
        config, tmp_path, monkeypatch, TextResponse(f"17.11.2026\n{MANIFEST_URL}"), FakeResponse(payload)
    )
    assert ok is True and ui.errors == []
    assert fake.urls == [DATE_URL, MANIFEST_URL]  # не db_update_url
    assert URL not in fake.urls and _count_manufacturers(db) == 3


def test_first_run_without_manifest_url_uses_settings_url(qapp, config, tmp_path, monkeypatch):
    payload = make_db(tmp_path / "remote.db", ("BMW", "Audi"))
    fake, db, ui, ok = _first_run(
        config, tmp_path, monkeypatch, TextResponse("17.11.2026"), FakeResponse(payload)
    )
    assert ok is True and fake.urls == [DATE_URL, URL] and _count_manufacturers(db) == 2


def test_first_run_manifest_unavailable_falls_back_to_settings_url(qapp, config, tmp_path, monkeypatch):
    payload = make_db(tmp_path / "remote.db", ("BMW",))
    fake, db, ui, ok = _first_run(
        config, tmp_path, monkeypatch, requests.ConnectionError("нет date.txt"), FakeResponse(payload)
    )
    assert ok is True and fake.urls == [DATE_URL, URL] and ui.errors == []


def test_first_run_ignores_manifest_date(qapp, config, tmp_path, monkeypatch):
    """Базы нет вовсе: даже «старая» дата в манифесте не отменяет скачивание."""
    payload = make_db(tmp_path / "remote.db", ("BMW",))
    fake, db, ui, ok = _first_run(
        config, tmp_path, monkeypatch, TextResponse(f"01.01.2000\n{MANIFEST_URL}"), FakeResponse(payload)
    )
    assert ok is True and fake.urls == [DATE_URL, MANIFEST_URL]


def test_real_thread_downloads_database_without_manifest(qapp, config, tmp_path, monkeypatch):
    payload = make_db(tmp_path / "remote.db", ("BMW", "Audi", "Chery"))
    fake = FakeGet(FakeResponse(payload))
    monkeypatch.setattr(db_update.requests, "get", fake)
    config.set("client", "db_date_url", "")
    db = tmp_path / "data" / "aprice.db"
    db.parent.mkdir()
    ui = RealThreadUi()
    assert ensure_database(config, db, ui) is True
    assert fake.urls == [URL] and ui.errors == [] and _count_manufacturers(db) == 3


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("нет сети"),
    requests.Timeout("таймаут"),
])
def test_real_thread_network_failure_blocks_start(qapp, config, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(db_update.requests, "get", FakeGet(failure))
    db = tmp_path / "aprice.db"
    ui = RealThreadUi()
    assert ensure_database(config, db, ui) is False
    assert not db.exists() and not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.part"))
    assert len(ui.errors) == 1 and "Проверьте подключение" in ui.errors[0]


def test_real_thread_broken_file_blocks_start(qapp, config, tmp_path, monkeypatch):
    monkeypatch.setattr(db_update.requests, "get", FakeGet(FakeResponse(b"<html>captcha</html>")))
    db = tmp_path / "aprice.db"
    ui = RealThreadUi()
    assert ensure_database(config, db, ui) is False
    assert not db.exists() and len(ui.errors) == 1


# ----------------------------------------------------------------------
# main(): без базы интерфейс не открывается
# ----------------------------------------------------------------------
def patch_main(monkeypatch, main_module, tmp_path, ensure):
    created: list = []
    monkeypatch.setattr(main_module, "Config", lambda: Config(tmp_path / "settings.json"))
    monkeypatch.setattr(main_module, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(main_module.paths, "ensure_runtime_dirs", lambda *a, **k: [])
    monkeypatch.setattr(main_module, "ensure_database", ensure)

    class Boom:
        def __init__(self, *a, **k):
            created.append(type(self).__name__)

    monkeypatch.setattr(main_module, "MainWindow", Boom)
    monkeypatch.setattr(main_module, "WindowManager", Boom)
    monkeypatch.setattr(main_module, "ClientController", Boom)
    return created


def test_main_returns_1_and_creates_no_window_when_download_fails(qapp, monkeypatch, tmp_path):
    from client import main as main_module

    created = patch_main(monkeypatch, main_module, tmp_path, lambda cfg: False)
    monkeypatch.setattr(QApplication, "exec", lambda self: pytest.fail("цикл событий не должен стартовать"))
    assert main_module.main(["aprice-client"]) == 1
    assert created == []  # ни окна, ни контроллера


def test_main_downloads_before_building_window(qapp, monkeypatch, tmp_path):
    from client import main as main_module

    order: list[str] = []
    created = patch_main(monkeypatch, main_module, tmp_path, lambda cfg: order.append("ensure") or True)

    class Win:
        def __init__(self, *a, **k):
            order.append("window")

        def show(self):
            order.append("show")

    class Ctl:
        def __init__(self, *a, **k):
            order.append("controller")

        def start_session(self):
            order.append("session")

        def shutdown(self):
            pass

    class Wm:
        def __init__(self, *a, **k):
            pass

        def apply(self, *a):
            pass

    monkeypatch.setattr(main_module, "MainWindow", Win)
    monkeypatch.setattr(main_module, "ClientController", Ctl)
    monkeypatch.setattr(main_module, "WindowManager", Wm)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert main_module.main(["aprice-client"]) == 0
    assert order[:2] == ["ensure", "window"] and "session" in order
