"""Тесты 5.2: Chromium ищется в ``browser/`` рядом с ``.exe`` (portable-режим Playwright)."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

from common import paths
from server import browser_check

ROOT = Path(__file__).resolve().parent.parent
ENV = "PLAYWRIGHT_BROWSERS_PATH"


@pytest.fixture(autouse=True)
def restore_env(monkeypatch):
    """Каждый тест возвращает окружение как было (переменную ставит и server.main при импорте)."""
    monkeypatch.setenv(ENV, os.environ.get(ENV, ""))
    yield


def test_configure_sets_env_to_browser_dir_next_to_base(tmp_path):
    result = paths.configure_playwright_browsers_path(tmp_path)
    assert result == tmp_path / "browser"
    assert os.environ[ENV] == str(tmp_path / "browser")


def test_configure_overrides_foreign_value(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV, "C:/Users/x/AppData/Local/ms-playwright")
    paths.configure_playwright_browsers_path(tmp_path)
    assert os.environ[ENV] == str(tmp_path / "browser")


def test_configure_default_uses_base_dir():
    assert paths.configure_playwright_browsers_path() == paths.BASE_DIR / "browser"
    assert os.environ[ENV] == str(paths.BASE_DIR / "browser")


def test_frozen_base_dir_is_exe_dir_for_browser(monkeypatch, tmp_path):
    exe = tmp_path / "Server" / "APrice-Server.exe"
    exe.parent.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    try:
        importlib.reload(paths)
        assert paths.BASE_DIR == exe.parent.resolve()
        assert paths.configure_playwright_browsers_path() == exe.parent.resolve() / "browser"
        assert os.environ[ENV] == str(exe.parent.resolve() / "browser")
    finally:
        monkeypatch.undo()
        importlib.reload(paths)
    assert paths.BASE_DIR == ROOT


def test_server_main_configures_browser_path_before_any_playwright_use():
    """server/main.py выставляет переменную при загрузке — до запуска Playwright."""
    text = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    assert "paths.configure_playwright_browsers_path()" in text
    # раньше первой функции/класса модуля, то есть при импорте, а не в main()
    assert text.index("configure_playwright_browsers_path()") < text.index("\ndef create_application")
    # а сами импорты playwright в проекте ленивые (не на уровне модуля)
    for rel in ("server/parsers/browser.py", "server/browser_check.py"):
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            if "import" in line and "playwright" in line:
                assert line.startswith((" ", "\t")), f"{rel}: импорт Playwright на уровне модуля"


def test_server_main_sets_env_on_import(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    sys.modules.pop("server.main", None)
    server_main = importlib.import_module("server.main")
    assert server_main.BROWSERS_DIR == paths.BASE_DIR / "browser"
    assert os.environ[ENV] == str(paths.BASE_DIR / "browser")


def test_playwright_looks_for_chromium_inside_browser_dir(tmp_path):
    """Настоящий Playwright (без браузера) ожидает chrome внутри заданного каталога."""
    browsers = paths.configure_playwright_browsers_path(tmp_path)
    expected = Path(browser_check.chromium_executable_path())
    assert expected.is_relative_to(browsers), expected
    assert not browser_check.chromium_installed()  # браузера нет
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"MZ")
    assert browser_check.chromium_installed()  # положили файл — сервер находит


def test_moved_distribution_still_finds_chromium(tmp_path):
    """Перенос папки Server в другое место (browser/ внутри) — браузер по-прежнему найден."""
    first = tmp_path / "A" / "Server"
    browsers = paths.configure_playwright_browsers_path(first)
    chrome = Path(browser_check.chromium_executable_path())
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"MZ")
    assert browser_check.chromium_installed()

    moved = tmp_path / "B" / "Moved"
    moved.parent.mkdir()
    shutil.move(str(first), str(moved))
    assert not browser_check.chromium_installed()  # env ещё указывает на старое место
    paths.configure_playwright_browsers_path(moved)  # новый запуск .exe в новом месте
    assert browser_check.chromium_installed()
    assert Path(browser_check.chromium_executable_path()).is_relative_to(moved / "browser")
    assert not browsers.exists()


def test_not_found_message_contains_path_and_instruction(monkeypatch):
    monkeypatch.setattr(browser_check, "chromium_executable_path",
                        lambda: r"D:\Apps\Server\browser\chromium-1200\chrome-win\chrome.exe")
    assert browser_check.not_found_message() == (
        r"Chromium не найден по пути D:\Apps\Server\browser\chromium-1200\chrome-win\chrome.exe. "
        "Установите его или положите в папку browser/ рядом с программой"
    )


def test_not_found_message_falls_back_to_browser_dir_when_playwright_is_broken(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_check, "chromium_executable_path", lambda: "")
    monkeypatch.setenv(ENV, str(tmp_path / "browser"))
    assert str(tmp_path / "browser") in browser_check.not_found_message()


def test_failed_install_shows_both_messages_with_path(monkeypatch):
    monkeypatch.setattr(browser_check, "chromium_executable_path", lambda: "X:/app/browser/chrome.exe")
    errors: list[str] = []

    class Ui:
        def run(self, work):
            return work()

        def show_error(self, message):
            errors.append(message)

    ok = browser_check.ensure_chromium(Ui(), is_installed=lambda: False,
                                       installer=lambda: (False, "SSL"))
    assert ok is False
    assert errors == [
        browser_check.INSTALL_FAILED_MESSAGE + "\n\n"
        "Chromium не найден по пути X:/app/browser/chrome.exe. "
        "Установите его или положите в папку browser/ рядом с программой"
    ]


def test_install_goes_into_portable_browser_dir(tmp_path):
    browsers = paths.configure_playwright_browsers_path(tmp_path)
    _argv, env = browser_check.build_install_command()
    assert env[ENV] == str(browsers)
