"""Тесты client.settings_dialog (4.4): значения <-> Config, обновление базы в потоке."""

import json
import os
import time

import pytest

import client.settings_dialog as sd
from client.db_update import DbUpdateError, UpdateResult
from client.settings_dialog import DbUpdateThread, SettingsDialog, url_error
from common.config import Config


class Boxes:
    """Подмена QMessageBox: собирает тексты вместо показа окон."""

    def __init__(self):
        self.critical_texts, self.info_texts = [], []

    def critical(self, parent, title, text, *a):
        self.critical_texts.append(text)

    def information(self, parent, title, text, *a):
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
    assert dialog.values() == {"max_pages_per_site": 10, "max_ads_per_site": 500,
                               "db_update_url": "", "db_max_age_days": 30}


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
    assert Config(config.path).load().get("client", "max_pages_per_site") == 10


@pytest.mark.parametrize("url", ["ftp://x/a.db", "mirror.test/a.db", "https://"])
def test_invalid_url_blocks_save(dialog, config, boxes, url):
    dialog.url_edit.setText(url)
    assert dialog.save() is False
    assert "http" in boxes.critical_texts[0]
    assert Config(config.path).load().get("client", "db_update_url") == ""
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
    return lambda url, target: DbUpdateThread(url, target, downloader=downloader)


def test_update_success_emits_signal_and_informs(qapp, config, db_file, boxes):
    calls, got = [], []
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RESULT, calls))
    d.database_updated.connect(got.append)
    d.url_edit.setText("https://mirror.test/aprice.db")
    d.update_button.click()
    assert calls == [("https://mirror.test/aprice.db", db_file)]
    assert got == [RESULT]
    assert "марок: 7" in d.update_status_label.text() and "марок: 7" in boxes.info_texts[0]
    assert d.update_button.isEnabled() and boxes.critical_texts == []


def test_update_uses_unsaved_url_field(qapp, config, db_file):
    calls = []
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RESULT, calls))
    d.url_edit.setText("https://unsaved.test/db")
    d.start_update()
    assert calls[0][0] == "https://unsaved.test/db"
    assert Config(config.path).load().get("client", "db_update_url") == ""  # в файл не писалось


def test_update_failure_shows_message_box(qapp, config, db_file, boxes):
    got = []
    d = SettingsDialog(config, db_path=db_file,
                       thread_factory=fake_thread(DbUpdateError("Не удалось скачать базу: 404"), []))
    d.database_updated.connect(got.append)
    d.url_edit.setText("https://mirror.test/x.db")
    d.start_update()
    assert boxes.critical_texts == ["Не удалось скачать базу: 404"]
    assert got == [] and d.update_button.isEnabled()
    assert d.update_status_label.text() == "Обновление не выполнено."


def test_unexpected_error_in_thread_is_reported_not_raised(qapp, config, db_file, boxes):
    d = SettingsDialog(config, db_path=db_file, thread_factory=fake_thread(RuntimeError("boom"), []))
    d.url_edit.setText("https://mirror.test/x.db")
    d.start_update()
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
