"""Настройка логирования проекта «АвтоПрайс».

Формирует конфигурацию :mod:`logging` по параметрам из ``settings.json``:

* общий файл ``logs/aprice_builder.log`` с ротацией (RotatingFileHandler);
* отдельный файл ошибок ``logs/aprice_builder_errors.log`` (уровень ERROR+);
* консоль (stderr);
* необязательный GUI-хендлер :class:`QtLogEmitter` (используется в Builder).

Формат: ``%(asctime)s | %(levelname)-8s | %(name)s | %(message)s``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from common import constants
from common.config import Config

LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"


class QtLogEmitter(logging.Handler):
    """Хэндлер, превращающий записи журнала в вызовы Python-колбэка.

    В GUI колбэком подключается сигнал ``QThread``/окна (сигнал → слот),
    что позволяет безопасно писать в виджет из фонового потока.

    Example:
        >>> emitter = QtLogEmitter()
        >>> emitter.connect(lambda text: log_view.appendPlainText(text))
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        """Инициализирует хэндлер и список подписчиков."""
        super().__init__(level=level)
        self._callbacks: list = []

    def connect(self, callback) -> None:
        """Подписывает колбэк ``callback(formatted_message: str)``."""
        self._callbacks.append(callback)

    def emit(self, record: logging.LogRecord) -> None:
        """Форматирует запись и передаёт её всем подписчикам."""
        try:
            message = self.format(record)
            for callback in list(self._callbacks):
                callback(message)
        except Exception:  # noqa: BLE001 — хэндлер не должен ронять приложение
            self.handleError(record)


def _ensure_dirs() -> None:
    """Создаёт каталоги логов, если они отсутствуют."""
    constants.LOGS_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(config: Optional[Config] = None, component: str = "builder") -> QtLogEmitter:
    """Настраивает корневой логгер приложения (идипотентно).

    Args:
        config: Загруженная конфигурация; если ``None`` — значения по умолчанию.
        component: Имя компонента для имён файлов (``builder`` / ``server`` / ``client``).

    Returns:
        QtLogEmitter: GUI-хэндлер, к которому можно подключиться сигналом.
    """
    _ensure_dirs()

    level_name = "INFO"
    max_bytes = 10 * 1024 * 1024
    backup_count = 3
    if config is not None:
        level_name = str(config.get("logging", "level", "INFO")).upper()
        max_bytes = int(config.get("logging", "max_file_size_mb", 10)) * 1024 * 1024
        backup_count = int(config.get("logging", "backup_count", 3))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    # Идемпотентность: при повторном вызове убираем собственные хэндлеры.
    for handler in list(root.handlers):
        if getattr(handler, "_aprice_managed", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Общий файл с ротацией.
    main_file = constants.LOGS_DIR / f"aprice_{component}.log"
    main_handler = RotatingFileHandler(
        main_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    main_handler.setFormatter(formatter)
    main_handler._aprice_managed = True  # type: ignore[attr-defined]
    root.addHandler(main_handler)

    # Файл только для ошибок (ERROR+).
    error_file = constants.LOGS_DIR / f"aprice_{component}_errors.log"
    error_handler = RotatingFileHandler(
        error_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler.addFilter(_ErrorOnlyFilter())
    error_handler._aprice_managed = True  # type: ignore[attr-defined]
    root.addHandler(error_handler)

    # Консоль.
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._aprice_managed = True  # type: ignore[attr-defined]
    root.addHandler(console)

    # GUI-эмиттер (подключается в builder/gui.py через сигнал).
    gui_handler = QtLogEmitter()
    gui_handler.setFormatter(formatter)
    gui_handler._aprice_managed = True  # type: ignore[attr-defined]
    root.addHandler(gui_handler)

    return gui_handler


class _ErrorOnlyFilter(logging.Filter):
    """Фильтр, пропускающий только записи уровня ERROR и выше."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        return record.levelno >= logging.ERROR


def get_logger(name: str) -> logging.Logger:
    """Возвращает логгер с указанным именем.

    Args:
        name: Имя логгера, обычно ``__name__`` модуля.
    """
    return logging.getLogger(name)
