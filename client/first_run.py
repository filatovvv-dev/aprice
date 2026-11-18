"""Автоскачивание справочника при первом запуске Client (подзадача 5.5).

База ``data/aprice.db`` не вшивается в дистрибутив Client. Если при старте её нет —
она скачивается с зеркала (``db_update_url``) в фоновом ``QThread`` под модальным
``QProgressDialog`` «Загрузка справочника... Это займет около минуты». Не удалось
(нет сети, битая ссылка) — ``QMessageBox.critical`` и основное окно не открывается,
потому что без базы марки и модели не загрузятся.

Логика (:func:`ensure_database`) не зависит от Qt-виджетов и тестируется с подменой
индикатора (:class:`DownloadUi`); Qt-реализация — :class:`QtDownloadUi`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from common import constants

logger = logging.getLogger("aprice.client.first_run")

TITLE = "АвтоПрайс — загрузка справочника"
PROGRESS_TEXT = "Загрузка справочника... Это займет около минуты"
NO_URL_MESSAGE = (
    "Справочник (aprice.db) не найден, а ссылка на его загрузку не задана. "
    "Укажите «Ссылку на зеркало» в config/settings.json (client.db_update_url) "
    "или положите aprice.db в папку data рядом с программой."
)
FAILED_MESSAGE = (
    "Не удалось загрузить справочник: {reason}\n\n"
    "Проверьте подключение к интернету и запустите программу снова. "
    "Без справочника марки и модели недоступны, поэтому программа будет закрыта."
)


class DownloadUi(Protocol):
    """Показ загрузки и ошибки (Qt-реализация — :class:`QtDownloadUi`)."""

    def run(self, thread: Any) -> tuple[bool, str]:
        """Запускает ``thread`` под индикатором; ``(успех, текст_ошибки)``."""

    def show_error(self, message: str) -> None:
        """Показывает критическую ошибку."""


class QtDownloadUi:
    """Модальный ``QProgressDialog`` на время работы ``QThread`` загрузки."""

    def __init__(self, parent=None) -> None:
        """Сохраняет родителя диалога (может быть ``None``)."""
        self._parent = parent

    def run(self, thread: Any) -> tuple[bool, str]:
        """Показывает диалог, запускает поток и ждёт сигнала ``succeeded``/``failed``."""
        from PyQt6.QtCore import QEventLoop, Qt
        from PyQt6.QtWidgets import QProgressDialog

        dialog = QProgressDialog(PROGRESS_TEXT, "", 0, 0, self._parent)  # 0..0 — «занято»
        dialog.setWindowTitle(TITLE)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)  # показать сразу
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setCancelButton(None)  # без отмены: без базы работать нельзя
        dialog.setValue(0)

        loop = QEventLoop()
        outcome: list[tuple[bool, str]] = []

        def on_progress(done: int, total: int) -> None:
            if total > 0:  # размер известен — обычная шкала
                dialog.setMaximum(total)
                dialog.setValue(min(done, total))

        def finish(ok: bool, reason: str = "") -> None:
            if not outcome:
                outcome.append((ok, reason))
            loop.quit()

        thread.progressed.connect(on_progress)
        thread.succeeded.connect(lambda _result: finish(True))
        thread.failed.connect(lambda reason: finish(False, reason))
        dialog.show()
        thread.start()
        loop.exec()
        thread.wait()
        dialog.close()
        return outcome[0] if outcome else (False, "загрузка прервана")

    def show_error(self, message: str) -> None:
        """Показывает ``QMessageBox.critical``."""
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(self._parent, TITLE, message)


def database_present(db_file: Path) -> bool:
    """Есть ли непустой файл базы."""
    try:
        return db_file.is_file() and db_file.stat().st_size > 0
    except OSError:
        return False


def ensure_database(
    config: Any,
    db_file: Optional[Path] = None,
    ui: Optional[DownloadUi] = None,
    *,
    thread_factory: Optional[Callable[[str, Path], Any]] = None,
) -> bool:
    """Гарантирует наличие базы: если файла нет — скачивает её с зеркала.

    Args:
        config: загруженный ``Config`` (читается ``client.db_update_url``).
        db_file: путь к базе (по умолчанию ``constants.DB_FILE``).
        ui: индикатор и сообщение об ошибке (по умолчанию Qt).
        thread_factory: ``(url, target) -> QThread`` (подменяется в тестах).

    Returns:
        bool: ``True`` — база есть (или скачана), можно открывать интерфейс;
        ``False`` — скачать не удалось, пользователю показана ошибка.
    """
    target = Path(db_file) if db_file is not None else constants.DB_FILE
    if database_present(target):
        return True
    ui = ui if ui is not None else QtDownloadUi()
    url = str(config.get("client", "db_update_url", "") or "").strip()
    if not url:
        logger.error("Базы %s нет и db_update_url не задан", target)
        ui.show_error(NO_URL_MESSAGE)
        return False
    logger.info("Базы %s нет — скачиваю с зеркала", target)
    if thread_factory is None:
        from client.settings_dialog import DbUpdateThread

        def thread_factory(u: str, t: Path) -> Any:  # noqa: E306
            return DbUpdateThread(u, t)  # без date_url: базы нет — качаем безусловно

    ok, reason = ui.run(thread_factory(url, target))
    if ok and database_present(target):
        logger.info("Справочник скачан: %s", target)
        return True
    logger.error("Не удалось скачать справочник: %s", reason)
    ui.show_error(FAILED_MESSAGE.format(reason=reason or "неизвестная ошибка"))
    return False
