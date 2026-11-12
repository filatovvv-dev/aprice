"""Защита от второго экземпляра APrice-Server (подзадача 3.6, MN-03).

:class:`SingleInstanceGuard` использует **тот же** :class:`IpcServer`, что и
приём заданий: порт ``127.0.0.1:8765`` занят ровно одним процессом, поэтому
«удалось начать прослушивание» ⇔ «мы первый экземпляр». Отдельный порт или
``QLocalServer`` не нужны: захват порта атомарен на уровне ОС, и между
проверкой и работой нет окна гонки.

Модуль также содержит :func:`show_centered_message` — окно сообщения,
всегда открывающееся по центру экрана и не запоминающее позицию (W-03).
Им пользуются и guard («Сервер уже запущен»), и ``server.main`` (ошибки
инициализации, MN-07).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from common.window_manager import WindowManager
from server.ipc import IpcServer

logger = logging.getLogger("aprice.server.single_instance")

#: Заголовок окна о втором экземпляре.
ALREADY_RUNNING_TITLE = "АвтоПрайс"
#: Текст окна о втором экземпляре (MN-03).
ALREADY_RUNNING_TEXT = "Сервер АвтоПрайс уже запущен"


def show_centered_message(title: str, text: str, *, critical: bool = False) -> None:
    """Показывает модальное окно сообщения строго по центру экрана (W-03).

    Позиция не сохраняется: окно не регистрируется в ``WindowManager``, а
    центрирование выполняется статическим :meth:`WindowManager.center` — тем
    же, что и ``center_error``, но без файла настроек и побочных эффектов.
    ``QMessageBox`` дорасчитывает размер уже после показа, поэтому
    центрирование повторяется через ``QTimer.singleShot(0, …)`` (внутри
    ``exec()`` таймер срабатывает после ``show``).

    Без ``QApplication`` (например, ``QCoreApplication`` в тестах) окно не
    создаётся — сообщение пишется только в лог.

    Args:
        title: заголовок окна.
        text: текст сообщения.
        critical: ``True`` — значок ошибки, иначе — предупреждения.
    """
    if not isinstance(QApplication.instance(), QApplication):
        logger.warning("Нет QApplication, окно не показано: %s — %s", title, text)
        return
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical if critical else QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    WindowManager.center(box)
    QTimer.singleShot(0, lambda: WindowManager.center(box))
    box.exec()


def show_already_running() -> None:
    """Окно «Сервер АвтоПрайс уже запущен» (по центру, W-03)."""
    show_centered_message(ALREADY_RUNNING_TITLE, ALREADY_RUNNING_TEXT)


class SingleInstanceGuard:
    """Гарантирует единственный запущенный экземпляр сервера.

    Пример:
        >>> guard = SingleInstanceGuard(IpcServer())
        >>> if not guard.acquire():
        ...     sys.exit(0)   # второй экземпляр: окно уже показано
    """

    def __init__(
        self,
        ipc_server: IpcServer,
        on_already_running: Optional[Callable[[], None]] = None,
    ) -> None:
        """Создаёт guard.

        Args:
            ipc_server: IPC-сервер приёма заданий (тот же, что и в работе).
            on_already_running: действие при занятом порте; по умолчанию —
                :func:`show_already_running`. Параметр нужен тестам, чтобы
                не открывать модальное окно.
        """
        self._ipc_server = ipc_server
        self._on_already_running = on_already_running or show_already_running
        self._acquired = False

    @property
    def acquired(self) -> bool:
        """``True``, если этот процесс — первый экземпляр и держит порт."""
        return self._acquired

    def acquire(self) -> bool:
        """Пытается стать первым экземпляром: ``ipc_server.start()``.

        Повторный вызов у уже захватившего guard возвращает ``True`` без
        повторного запуска.

        Returns:
            bool: ``True`` — порт захвачен, можно работать; ``False`` —
            порт занят другим экземпляром (окно о втором экземпляре уже
            показано, вызывающий должен завершиться с кодом ``0``).
        """
        if self._acquired:
            return True
        if self._ipc_server.start():
            self._acquired = True
            return True
        logger.warning("Порт IPC занят: сервер уже запущен в другом процессе")
        try:
            self._on_already_running()
        except Exception:  # noqa: BLE001 - окно не должно мешать выходу
            logger.exception("Не удалось показать окно о втором экземпляре")
        return False

    def release(self) -> None:
        """Освобождает порт (``ipc_server.stop()``); идемпотентно."""
        if not self._acquired:
            return
        self._acquired = False
        self._ipc_server.stop()
