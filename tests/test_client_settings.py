"""Тесты client.settings_dialog (4.4): значения <-> Config, обновление базы в потоке."""

import json
import os
import time

import pytest
from PyQt6.QtWidgets import QApplication

import client.settings_dialog as sd
from client.db_update import DbUpdateError, UpdateResult
from client.settings_dialog import DbUpdateThread, SettingsDialog, url_error
from common import constants
from common.config import Config


DEFAULT_URL = constants.DEFAULT_SETTINGS["client"]["db_update_url"]


def wait_for(condition, timeout: float = 5.0) -> None:
    """Прокручивает события Qt, пока не выполнится ``condition`` (или не истечёт таймаут).

    В настоящем Qt ``QThread`` работает асинхронно: сигналы потока доставляются в
    GUI-поток через очередь событий, поэтому проверять результат сразу после
    ``start_update()`` нельзя. Хелпер не зависит от ``QtTest``/``pytest-qt``.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("Условие не выполнено за отведённое время (поток не завершился?)")
        QApplication.processEvents()
        time.sleep(0.005)


def finish_thread(d: SettingsDialog, timeout_ms: int = 5000) -> None:
    """Дожидается окончания потока обновления, чтобы он не был уничтожен «на ходу»."""
    if d._thread is not None:
        assert d._thread.wait(timeout_ms)
    QApplication.processEvents()


class Boxes:
    """Подмена QMessageBox: собирает тексты вместо показа окон."""

    def __init__(self):
        self.critical_texts, self.info_texts, self.info_titles = [], [], []

    def critical(self, parent, title, text, *a):
        self.critical_texts.append(text)

    def information(self, parent, title, text, *a):
        self.info_titles.append(title)
        self.info_texts.append(text)


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    recorded = Boxes()
    monkeypatch.setattr(sd, "QMessageBox", recorded)
    return recorded


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "config" / "settings.json").load()


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "aprice.db"
    path.write_bytes(b"x")
    return path


@pytest.fixture
def dialog(qapp, config, db_file):
    return SettingsDialog(config, db_path=db_file)


def test_dialog_loads_defaults_from_config(dialog):
    assert dialog.values() == {"max_pages_per_site": 2, "max_ads_per_site": 50,
                               "db_update_url": constants.DEFAULT_SETTINGS["client"]["db_update_url"],
                               "db_date_url": constants.DEFAULT_SETTINGS["client"]["db_date_url"],
                               "db_max_age_days": 180}


def test_dialog_loads_existing_values(qapp, config, db_file):
    config.set("client", "max_pages_per_site", 3)
    config.set("client", "db_update_url", "https://mirror.test/a.db")
    config.set("client", "db_max_age_days", 7)
    d = SettingsDialog(config, db_path=db_file)
    assert d.max_pages_spin.value() == 3 and d.url_edit.text() == "https://mirror.test/a.db"
    assert d.age_spin.value() == 7


def test_save_writes_config_file(dialog, config):
    dialog.max_pages_spin.setValue(4)
    dialog.max_ads_spin.setValue(250)
    dialog.url_edit.setText("  https://mirror.test/aprice.db  ")
    dialog.age_spin.setValue(14)
    assert dialog.save() is True
    assert dialog.result() == 1  # диалог принят
    saved = json.loads(config.path.read_text(encoding="utf-8"))["client"]
    assert saved["max_pages_per_site"] == 4 and saved["max_ads_per_site"] == 250
    assert saved["db_update_url"] == "https://mirror.test/aprice.db"  # пробелы обрезаны
    assert saved["db_max_age_days"] == 14
    assert saved["show_vpn_warning"] is True  # остальные ключи секции не потеряны
    reloaded = Config(config.path).load()
    assert reloaded.get("client", "max_ads_per_site") == 250


def test_cancel_does_not_touch_config(dialog, config):
    dialog.max_pages_spin.setValue(99)
    dialog.cancel_button.click()
    assert dialog.result() == 0
    assert Config(config.path).load().get("client", "max_pages_per_site") == 2


@pytest.mark.parametrize("url", ["ftp://x/a.db", "mirror.test/a.db", "https://"])
def test_invalid_url_blocks_save(dialog, config, boxes, url):
    dialog.url_edit.setText(url)
    assert dialog.save() is False
    assert "http" in boxes.critical_texts[0]
    assert Config(config.path).load().get("client", "db_update_url") == DEFAULT_URL
    assert dialog.result() == 0


def test_empty_url_is_allowed(dialog):
    dialog.url_edit.setText("")
    assert dialog.save() is True


def test_save_failure_shows_error(dialog, config, boxes, monkeypatch):
    monkeypatch.setattr(config, "save", lambda: (_ for _ in ()).throw(OSError("диск только для чтения")))
    assert dialog.save() is False
    assert "диск только для чтения" in boxes.critical_texts[0]


def test_url_error_helper():
    assert url_error("") is None and url_error("   ") is None
    assert url_error("https://a.b/c") is None and url_error("http://a.b") is None
    assert url_error("a.b/c") and url_error("ftp://a.b")


def test_age_label_fresh_stale_missing(dialog, db_file, tmp_path):
    assert "0 дн." in dialog.db_age_label.text() and dialog.db_age_label.styleSheet() == ""
    old = time.time() - 40 * 86400
    os.utime(db_file, (old, old))
    dialog.age_spin.setValue(20)
    assert "устарел" in dialog.db_age_label.text() and "red" in dialog.db_age_label.styleSheet()
    dialog.age_spin.setValue(60)  # порог выше возраста — снова «свежая»
    assert "устарел" not in dialog.db_age_label.text()
    missing = SettingsDialog(Config(tmp_path / "c.json"), db_path=tmp_path / "nope.db")
    assert "не найден" in missing.db_age_label.text()


# ---------------------------------------------------------------- обновление базы
RESULT = UpdateResult(path=None, backup=None, size=10, manufacturers=7)


def fake_thread(outcome, record):
    """Фабрика потока, чей downloader возвращает/бросает ``outcome``."""
    def downloader(url, target, progress=None):
        record.append((url, target))
        if progress:
            progress(1_048_576, 2_097_152)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    # date_url всегда пустой: тесты 4.4 проверяют скачивание без проверки date.txt (4.5 — ниже).
    return lambda url, target, date_url="": DbUpdateThread(url, target, downloader=downloader)


def test_update_success_emits_signal_and_informs(qapp, config, db_file, boxes):
    calls, got = [], []
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RESULT, calls))
    d.database_updated.connect(got.append)
    d.url_edit.setText("https://mirror.test/aprice.db")
    d.update_button.click()
    wait_for(lambda: calls and got)  # поток асинхронный: ждём downloader и сигнал
    finish_thread(d)
    assert calls == [("https://mirror.test/aprice.db", db_file)]
    assert got == [RESULT]
    assert "марок: 7" in d.update_status_label.text() and "марок: 7" in boxes.info_texts[0]
    assert d.update_button.isEnabled() and boxes.critical_texts == []


def test_update_uses_unsaved_url_field(qapp, config, db_file):
    calls = []
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RESULT, calls))
    d.url_edit.setText("https://unsaved.test/db")
    d.start_update()
    wait_for(lambda: calls)
    finish_thread(d)
    assert calls[0][0] == "https://unsaved.test/db"
    assert Config(config.path).load().get("client", "db_update_url") == DEFAULT_URL  # в файл не писалось


def test_update_failure_shows_message_box(qapp, config, db_file, boxes):
    got = []
    d = SettingsDialog(config, db_path=db_file,
                       thread_factory=fake_thread(DbUpdateError("Не удалось скачать базу: 404"), []))
    d.database_updated.connect(got.append)
    d.url_edit.setText("https://mirror.test/x.db")
    d.start_update()
    wait_for(lambda: boxes.critical_texts)
    finish_thread(d)
    assert boxes.critical_texts == ["Не удалось скачать базу: 404"]
    assert got == [] and d.update_button.isEnabled()
    assert d.update_status_label.text() == "Обновление не выполнено."


def test_unexpected_error_in_thread_is_reported_not_raised(qapp, config, db_file, boxes):
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RuntimeError("boom"), []))
    d.url_edit.setText("https://mirror.test/x.db")
    d.start_update()
    wait_for(lambda: boxes.critical_texts)
    finish_thread(d)
    assert "Непредвиденная ошибка: boom" in boxes.critical_texts[0]


@pytest.mark.parametrize("url", ["", "ftp://x/y"])
def test_update_without_valid_url_does_not_start(qapp, config, db_file, boxes, url):
    calls = []
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RESULT, calls))
    d.url_edit.setText(url)
    d.start_update()
    assert calls == [] and len(boxes.critical_texts) == 1


def test_progress_text(dialog):
    dialog._on_progress(1_048_576, 2_097_152)
    assert dialog.update_status_label.text() == "Загрузка… 1.0 из 2.0 МБ"
    dialog._on_progress(524_288, -1)
    assert dialog.update_status_label.text() == "Загрузка… 0.5 МБ"


def test_dialog_cannot_close_or_save_while_updating(dialog, monkeypatch):
    class Busy:
        def isRunning(self):
            return True
    dialog._thread = Busy()
    dialog.reject()
    assert dialog.result() == 0 and "Дождитесь" in dialog.update_status_label.text()
    assert dialog.save() is False
    dialog.start_update()  # повторный запуск игнорируется (иначе упал бы на Busy)


def test_thread_passes_progress_and_target(tmp_path):
    seen = []
    thread = DbUpdateThread("https://x/y", tmp_path / "t.db",
                            downloader=lambda url, target, progress=None: (
                                progress(5, None), RESULT)[1])
    thread.progressed.connect(lambda done, total: seen.append((done, total)))
    thread.run()
    assert seen == [(5, -1)]


# ----------------------------------------------------------------------
# 4.5: умное обновление (date.txt)
# ----------------------------------------------------------------------
from datetime import datetime  # noqa: E402

import requests  # noqa: E402

from client import db_update  # noqa: E402
from tests.test_db_update import FakeGet, FakeResponse, TextResponse, make_db  # noqa: E402

DB_URL = "https://mirror.test/aprice.db"
DATE_URL = "https://mirror.test/date.txt"
LOCAL_DATE = datetime(2026, 11, 17, 12, 0, 0)


@pytest.fixture
def smart(qapp, config, tmp_path, monkeypatch):
    """Диалог с реальным потоком и проверкой даты; сеть подменена ``requests.get``."""
    db = tmp_path / "aprice.db"
    make_db(db, ("OLD",))
    ts = LOCAL_DATE.timestamp()
    os.utime(db, (ts, ts))
    d = SettingsDialog(config, db_path=db)
    d.url_edit.setText(DB_URL)
    d.date_url_edit.setText(DATE_URL)

    def install(*responses):
        fake = FakeGet(*responses)
        monkeypatch.setattr(db_update.requests, "get", fake)
        return fake

    d.install = install
    return d, db


def run_update(d, done):
    d.start_update()
    wait_for(done)
    finish_thread(d)


def test_settings_dialog_skips_download_if_up_to_date(smart, boxes):
    d, db = smart
    before = db.read_bytes()
    fake = d.install(TextResponse("2026-11-17T12:00:00"))  # тот же момент, что у локальной базы
    got = []
    d.database_updated.connect(got.append)
    run_update(d, lambda: boxes.info_texts)
    assert fake.urls == [DATE_URL]  # aprice.db не запрашивался
    assert boxes.info_titles == ["Обновления не требуются"]
    assert boxes.info_texts == ["У вас уже установлена самая свежая версия базы."]
    assert db.read_bytes() == before and got == [] and boxes.critical_texts == []
    assert d.update_button.isEnabled()


def test_settings_dialog_skips_download_if_remote_older(smart, boxes):
    d, db = smart
    fake = d.install(TextResponse("2026-11-01T00:00:00"))
    run_update(d, lambda: boxes.info_texts)
    assert fake.urls == [DATE_URL] and boxes.info_titles == ["Обновления не требуются"]


def test_settings_dialog_downloads_if_remote_newer(smart, boxes, tmp_path):
    d, db = smart
    payload = make_db(tmp_path / "remote.db", ("BMW", "Audi"))
    fake = d.install(TextResponse("2026-11-18T09:30:00"), FakeResponse(payload))
    got = []
    d.database_updated.connect(got.append)
    run_update(d, lambda: got)
    assert fake.urls == [DATE_URL, DB_URL]
    assert got[0].manufacturers == 2
    assert abs(db.stat().st_mtime - datetime(2026, 11, 18, 9, 30).timestamp()) < 1
    assert "обновлена" in boxes.info_texts[0] and boxes.info_titles == ["Настройки"]
    # повторное нажатие: дата уже совпадает — второй раз базу не качаем
    boxes.info_texts.clear()
    fake2 = d.install(TextResponse("2026-11-18T09:30:00"))
    run_update(d, lambda: boxes.info_texts)
    assert fake2.urls == [DATE_URL]


def test_date_check_failure_is_reported_and_nothing_downloaded(smart, boxes):
    d, db = smart
    fake = d.install(requests.ConnectionError("нет сети"))
    before = db.read_bytes()
    run_update(d, lambda: boxes.critical_texts)
    assert "нет сети" in boxes.critical_texts[0]
    assert fake.urls == [DATE_URL] and db.read_bytes() == before and boxes.info_texts == []
    assert d.update_button.isEnabled()


def test_unrecognized_date_means_up_to_date_without_download(smart, boxes):
    """5.5: нераспознанная дата в date.txt — «обновление не требуется», база не качается."""
    d, db = smart
    before = db.read_bytes()
    fake = d.install(TextResponse("<html>captcha</html>"))
    run_update(d, lambda: boxes.info_texts)
    assert fake.urls == [DATE_URL] and db.read_bytes() == before
    assert boxes.info_titles == ["Обновления не требуются"] and boxes.critical_texts == []


def test_russian_date_in_date_file_triggers_download(smart, boxes, tmp_path):
    d, db = smart
    payload = make_db(tmp_path / "remote.db", ("BMW", "Audi"))
    fake = d.install(TextResponse("18.11.2026"), FakeResponse(payload))  # локальная — 17.11.2026 12:00
    got = []
    d.database_updated.connect(got.append)
    run_update(d, lambda: got)
    assert fake.urls == [DATE_URL, DB_URL]
    assert abs(db.stat().st_mtime - datetime(2026, 11, 18).timestamp()) < 1


def test_empty_date_url_downloads_without_check(smart, boxes, tmp_path):
    d, db = smart
    d.date_url_edit.setText("")
    payload = make_db(tmp_path / "remote.db", ("BMW",))
    fake = d.install(FakeResponse(payload))
    got = []
    d.database_updated.connect(got.append)
    run_update(d, lambda: got)
    assert fake.urls == [DB_URL]


def test_date_url_is_saved_and_validated(dialog, config, boxes):
    dialog.date_url_edit.setText("  https://mirror.test/date.txt ")
    assert dialog.save() is True
    assert Config(config.path).load().get("client", "db_date_url") == "https://mirror.test/date.txt"


def test_invalid_date_url_blocks_save_and_update(dialog, config, boxes):
    dialog.date_url_edit.setText("ftp://x/date.txt")
    assert dialog.save() is False
    dialog.url_edit.setText("https://mirror.test/a.db")
    dialog.start_update()
    assert len(boxes.critical_texts) == 2 and dialog._thread is None


def test_default_limits_5_6():
    from common import constants
    client = constants.DEFAULT_SETTINGS["client"]
    assert (client["max_pages_per_site"], client["max_ads_per_site"]) == (2, 50)


def test_default_autostart_server_enabled_5_6():
    from common import constants
    assert constants.DEFAULT_SETTINGS["client"]["autostart_server"] is True
