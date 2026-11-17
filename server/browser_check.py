"""Проверка и установка Chromium для Playwright (этап 5.1).

Chromium **не вшивается** в сборку (это сотни мегабайт). При первом запуске
Server проверяет ``pw.chromium.executable_path`` и, если браузера нет, запускает
``playwright install chromium`` — в фоновом потоке, с ``QProgressDialog``.

Логика (проверка, команда, установка) не зависит от Qt и тестируется на моках;
Qt-часть (:class:`QtInstallUi`) импортируется лениво.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Protocol

logger = logging.getLogger("aprice.server.browser_check")

INSTALL_FAILED_MESSAGE = (
    "Не удалось установить браузер. Проверьте соединение или установите вручную"
)
INSTALL_TITLE = "АвтоПрайс — установка браузера"
INSTALL_PROGRESS_TEXT = (
    "Первый запуск: скачивается браузер Chromium (около 150 МБ).\n"
    "Пожалуйста, подождите — это происходит один раз."
)
INSTALL_TIMEOUT_SEC = 1800  # 30 минут на медленном канале


def chromium_installed() -> bool:
    """Проверяет, что исполняемый файл Chromium из Playwright существует.

    Returns:
        bool: ``True`` — браузер установлен; ``False`` — его нет или Playwright
        недоступен/упал при проверке.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
        return bool(executable) and Path(executable).exists()
    except Exception:  # noqa: BLE001 - любая ошибка = «считаем, что браузера нет»
        logger.exception("Не удалось проверить наличие Chromium")
        return False


def build_install_command() -> tuple[list[str], dict[str, str]]:
    """Формирует команду установки Chromium.

    В собранном ``.exe`` консольной команды ``playwright`` в ``PATH`` нет, поэтому
    используется встроенный драйвер Playwright (``node`` + ``cli.js``), который
    PyInstaller кладёт в сборку через ``collect_all('playwright')``. Если драйвер
    найти не удалось, применяется прямая команда ``playwright install chromium``.

    Returns:
        tuple: ``(argv, env)`` для :func:`subprocess.run`.
    """
    env = dict(os.environ)
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        driver = compute_driver_executable()
        node, cli = (driver if isinstance(driver, tuple) else (str(driver), None))
        argv = [str(node)] + ([str(cli)] if cli else []) + ["install", "chromium"]
        env = dict(get_driver_env())
        return argv, env
    except Exception:  # noqa: BLE001 - запасной вариант из ТЗ
        logger.warning("Драйвер Playwright не найден, использую команду 'playwright'")
        return ["playwright", "install", "chromium"], env


def install_chromium(
    runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> tuple[bool, str]:
    """Устанавливает Chromium (``playwright install chromium``).

    Args:
        runner: функция запуска процесса (подменяется в тестах).

    Returns:
        tuple: ``(успех, подробности_для_лога)``.
    """
    argv, env = build_install_command()
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "env": env,
        "timeout": INSTALL_TIMEOUT_SEC,
    }
    if sys.platform == "win32":  # без мигающего консольного окна
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = runner(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Запуск установки Chromium не удался: %s", exc)
        return False, str(exc)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        logger.error("playwright install завершился с кодом %s: %s", result.returncode, output)
        return False, output
    logger.info("Chromium установлен")
    return True, output


class InstallUi(Protocol):
    """Интерфейс показа процесса установки (Qt-реализация — :class:`QtInstallUi`)."""

    def run(self, work: Callable[[], tuple[bool, str]]) -> Optional[tuple[bool, str]]:
        """Выполняет ``work`` с индикатором; ``None`` — пользователь отменил."""

    def show_error(self, message: str) -> None:
        """Показывает сообщение об ошибке."""


class QtInstallUi:
    """Индикатор на ``QProgressDialog``: работа идёт в ``QThread``, окно — «занято»."""

    def __init__(self, parent=None) -> None:
        """Сохраняет родительский виджет (может быть ``None``)."""
        self._parent = parent

    def run(self, work: Callable[[], tuple[bool, str]]) -> Optional[tuple[bool, str]]:
        """Показывает ``QProgressDialog`` и ждёт завершения ``work`` в потоке."""
        from PyQt6.QtCore import QEventLoop, QThread, Qt, pyqtSignal
        from PyQt6.QtWidgets import QProgressDialog

        class _Worker(QThread):
            done = pyqtSignal(bool, str)

            def run(self) -> None:  # noqa: D102
                try:
                    ok, details = work()
                except Exception as exc:  # noqa: BLE001
                    ok, details = False, str(exc)
                self.done.emit(ok, details)

        dialog = QProgressDialog(INSTALL_PROGRESS_TEXT, "Отмена", 0, 0, self._parent)
        dialog.setWindowTitle(INSTALL_TITLE)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)  # показать сразу, не через 4 с
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setCancelButton(None)  # прерывать скачивание на полпути нельзя
        dialog.setValue(0)

        loop = QEventLoop()
        outcome: list[tuple[bool, str]] = []
        worker = _Worker()

        def _finished(ok: bool, details: str) -> None:
            outcome.append((ok, details))
            loop.quit()

        worker.done.connect(_finished)
        dialog.show()
        worker.start()
        loop.exec()
        worker.wait()
        dialog.close()
        return outcome[0] if outcome else None

    def show_error(self, message: str) -> None:
        """Показывает ``QMessageBox.warning`` с текстом ошибки."""
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.warning(self._parent, INSTALL_TITLE, message)


def ensure_chromium(
    ui: Optional[InstallUi] = None,
    *,
    is_installed: Callable[[], bool] = chromium_installed,
    installer: Callable[[], tuple[bool, str]] = install_chromium,
) -> bool:
    """Гарантирует наличие Chromium: проверяет и при необходимости устанавливает.

    Args:
        ui: индикатор установки (по умолчанию Qt-диалог).
        is_installed: проверка наличия браузера (подменяется в тестах).
        installer: установка браузера (подменяется в тестах).

    Returns:
        bool: ``True``, если Chromium доступен после вызова. При неудаче
        пользователю показано :data:`INSTALL_FAILED_MESSAGE`.
    """
    if is_installed():
        return True
    logger.info("Chromium не найден — запускаю установку")
    ui = ui if ui is not None else QtInstallUi()
    result = ui.run(installer)
    if result is not None and result[0] and is_installed():
        return True
    logger.error("Chromium не установлен: %s", result[1] if result else "отменено")
    ui.show_error(INSTALL_FAILED_MESSAGE)
    return False
