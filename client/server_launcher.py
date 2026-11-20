"""Автозапуск APrice-Server из Client (подзадача 5.6).

Client при старте проверяет, слушает ли Server ``127.0.0.1:8765``; если нет — запускает
``APrice-Server.exe`` (``subprocess.Popen``) и ждёт готовности с таймаутом. Завершение
Server при закрытии Client уже обеспечивает «сеанс» (``get_active_task(session=True)``):
когда сеанс закрыт и активных заданий нет, Server выходит сам; при активном задании
продолжает работать (:meth:`server.main.ServerApp._on_client_disconnected`).

Модуль не зависит от Qt и тестируется на моках.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from common import constants, paths

logger = logging.getLogger("aprice.client.server_launcher")

SERVER_EXE_NAME = "APrice-Server"
#: Сколько секунд ждать, пока Server начнёт принимать подключения.
START_TIMEOUT_SEC = 30.0
POLL_INTERVAL_SEC = 0.25


def is_server_running(
    host: str = constants.IPC_HOST, port: int = constants.IPC_PORT, timeout: float = 0.5
) -> bool:
    """``True``, если по адресу Server принимаются TCP-подключения."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _exe_names() -> tuple[str, ...]:
    return (f"{SERVER_EXE_NAME}.exe", SERVER_EXE_NAME)


def find_server_command(base_dir: Optional[Path] = None, frozen: Optional[bool] = None) -> Optional[tuple[list[str], Path]]:
    """Ищет, чем запускать Server.

    Порядок: ``APrice-Server.exe`` рядом с Client (единый ``dist/``, 5.6) → в
    ``../Server`` (старая раскладка из трёх папок) → в режиме разработки ``dist/`` и
    ``dist/Server`` → ``python -m server.main`` из корня проекта.

    Returns:
        ``(argv, рабочий_каталог)`` либо ``None`` (в собранной версии exe не найден).
    """
    base = Path(base_dir) if base_dir is not None else paths.BASE_DIR
    frozen = paths.is_frozen() if frozen is None else frozen
    candidates: list[Path] = [base, base.parent / "Server"]
    if not frozen:
        candidates += [base / "dist", base / "dist" / "Server"]
    for folder in candidates:
        for name in _exe_names():
            exe = folder / name
            if exe.is_file():
                return [str(exe)], folder
    if not frozen and (base / "server" / "main.py").is_file():
        return [sys.executable, "-m", "server.main"], base
    return None


def start_server(command: Sequence[str], cwd: Path, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Запускает Server отдельным процессом (он переживает Client, если есть задание)."""
    kwargs: dict = {"cwd": str(cwd), "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)  # скрыто: без консольного окна
        )
    else:
        kwargs["start_new_session"] = True
    try:
        popen(list(command), **kwargs)
    except OSError as exc:
        logger.error("Не удалось запустить Server (%s): %s", command, exc)
        return False
    logger.info("Server запущен: %s", command)
    return True


def wait_for_server(
    timeout: float = START_TIMEOUT_SEC,
    *,
    probe: Callable[[], bool] = is_server_running,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_wait: Optional[Callable[[], None]] = None,
) -> bool:
    """Ждёт готовности Server; ``on_wait`` вызывается на каждом шаге (обработка событий Qt)."""
    deadline = clock() + timeout
    while True:
        if probe():
            return True
        if clock() >= deadline:
            return False
        if on_wait is not None:
            on_wait()
        sleep(POLL_INTERVAL_SEC)


def ensure_server(
    config=None,
    *,
    probe: Callable[[], bool] = is_server_running,
    finder: Callable[[], Optional[tuple[list[str], Path]]] = find_server_command,
    starter: Callable[[Sequence[str], Path], bool] = start_server,
    waiter: Callable[..., bool] = wait_for_server,
    on_wait: Optional[Callable[[], None]] = None,
) -> bool:
    """Гарантирует, что Server запущен: проверяет порт и при необходимости стартует его.

    Отключается настройкой ``client.autostart_server = false``. Неудача не фатальна:
    Client работает, а сеанс откроется при первой отправке задания.

    Returns:
        bool: ``True``, если Server принимает подключения.
    """
    if config is not None and not config.get("client", "autostart_server", True):
        logger.info("Автозапуск Server отключён настройкой")
        return probe()
    if probe():
        return True
    found = finder()
    if found is None:
        logger.warning("APrice-Server не найден — запустите его вручную")
        return False
    argv, cwd = found
    if not starter(argv, cwd):
        return False
    ready = waiter(on_wait=on_wait)
    if not ready:
        logger.warning("Server не ответил за %.0f с (возможно, идёт установка Chromium)", START_TIMEOUT_SEC)
    return ready
