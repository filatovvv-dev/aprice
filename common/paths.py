"""Пути приложения для обычного запуска и для сборки PyInstaller (этап 5.1).

Модуль намеренно не зависит от Qt и от остальных модулей проекта, чтобы его
можно было импортировать из ``common.constants`` без циклов.

* ``BASE_DIR`` — каталог, рядом с которым живут ``config/``, ``logs/`` и
  ``data/``: каталог ``.exe`` в собранной версии, корень проекта — в разработке.
* ``RESOURCE_DIR`` — где лежат вшитые ресурсы: ``sys._MEIPASS`` (в режиме
  ``--onedir`` это ``<папка с exe>/_internal``), в разработке — корень проекта.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Имя файла справочной базы Builder'а.
REFERENCE_DB_NAME = "aprice.db"


def is_frozen() -> bool:
    """``True``, если приложение запущено из сборки PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def get_base_dir() -> Path:
    """Возвращает каталог ``.exe`` (frozen) либо корень проекта.

    Returns:
        Path: базовый каталог для ``config/``, ``logs/``, ``data/``.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # common/paths.py -> common -> корень проекта
    return Path(__file__).resolve().parent.parent


def get_resource_dir() -> Path:
    """Возвращает каталог вшитых ресурсов (``sys._MEIPASS`` в frozen-режиме)."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return get_base_dir()
    return get_base_dir()


BASE_DIR: Path = get_base_dir()
RESOURCE_DIR: Path = get_resource_dir()


def ensure_runtime_dirs(base_dir: Optional[Path] = None) -> list[Path]:
    """Создаёт ``config/``, ``logs/`` и ``data/`` рядом с ``.exe``.

    Args:
        base_dir: базовый каталог (по умолчанию :data:`BASE_DIR`).

    Returns:
        list[Path]: список каталогов, которые гарантированно существуют.
    """
    root = Path(base_dir) if base_dir is not None else BASE_DIR
    created: list[Path] = []
    for name in ("config", "logs", "data"):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)
    return created


#: Имя переменной окружения Playwright и каталог портативного браузера (5.2).
PLAYWRIGHT_BROWSERS_ENV = "PLAYWRIGHT_BROWSERS_PATH"
BROWSER_DIR_NAME = "browser"


def configure_playwright_browsers_path(base_dir: Optional[Path] = None) -> Path:
    """Направляет Playwright на ``browser/`` рядом с ``.exe`` (portable-режим, 5.2).

    Без этого Playwright ищет/скачивает Chromium в ``%LOCALAPPDATA%\\ms-playwright``
    — собранный каталог становится непереносимым, а запись туда блокируют антивирусы.
    Вызывать нужно до первого запуска Playwright (его импорты в проекте ленивые).
    Значение выставляется безусловно: прежнее значение переменной (например, от
    другой установки) нарушило бы переносимость сборки.

    Args:
        base_dir: базовый каталог (по умолчанию :data:`BASE_DIR`).

    Returns:
        Path: каталог браузеров (он может ещё не существовать).
    """
    path = (Path(base_dir) if base_dir is not None else BASE_DIR) / BROWSER_DIR_NAME
    os.environ[PLAYWRIGHT_BROWSERS_ENV] = str(path)
    return path


def bootstrap_reference_db(
    target: Optional[Path] = None,
    resource_dir: Optional[Path] = None,
) -> bool:
    """Копирует вшитую ``data/aprice.db`` рядом с ``.exe`` при первом запуске.

    Копирование выполняется только если целевого файла нет: существующая
    (возможно, обновлённая пользователем) база никогда не перезаписывается.
    Вне сборки PyInstaller вшитый ресурс и рабочая база — один и тот же файл,
    поэтому функция ничего не делает.

    Args:
        target: целевой файл (по умолчанию ``BASE_DIR/data/aprice.db``).
        resource_dir: каталог ресурсов (по умолчанию :data:`RESOURCE_DIR`).

    Returns:
        bool: ``True``, если файл был скопирован.
    """
    target_path = Path(target) if target is not None else BASE_DIR / "data" / REFERENCE_DB_NAME
    if target_path.exists():
        return False
    source = (Path(resource_dir) if resource_dir is not None else RESOURCE_DIR) / "data" / REFERENCE_DB_NAME
    if not source.is_file():
        logger.info("Вшитая база %s не найдена — будет создана пустая схема", source)
        return False
    if source.resolve() == target_path.resolve():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(target_path.name + ".tmp")
    shutil.copyfile(source, tmp_path)
    tmp_path.replace(target_path)  # атомарно: не оставляем «половинную» базу
    logger.info("Справочная база скопирована из ресурсов: %s -> %s", source, target_path)
    return True
