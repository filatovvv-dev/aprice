"""Миграции схемы SQLite-базы справочников APrice (задача №2, п. 1.3).

Актуальная версия схемы — :data:`common.constants.SCHEMA_VERSION` (= 2).

Изменения v1 -> v2:

* таблице ``configurations`` добавлено ограничение
  ``UNIQUE (generation_id, transmission, fuel_type)`` — бизнес-ключ
  конфигурации; это позволяет перейти с «SELECT перед INSERT» на честный
  UPSERT (``INSERT ... ON CONFLICT ... DO UPDATE``);
* при миграции существующие дубликаты (полностью совпадающие строки)
  схлопываются в одну (``INSERT OR IGNORE``);
* перед любым изменением схемы обязательно создаётся резервная копия
  ``data/aprice.db.bak-v1`` (требование ТЗ-разработчика, промпт №2, п. 6);
* после успешной миграции ``metadata.schema_version`` обновляется до ``2``.

Функция :func:`migrate_to_v2` идемпотентна: повторный вызов на уже
мигрированной базе не делает ничего.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from common import constants

logger = logging.getLogger(__name__)

# Схема таблицы configurations версии 2 (с UNIQUE-бизнес-ключом).
_CONFIGURATIONS_V2_SQL = """
CREATE TABLE configurations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id      INTEGER NOT NULL,
    transmission       TEXT,
    fuel_type          TEXT,
    engine_volume_from REAL,
    engine_volume_to   REAL,
    power_from         INTEGER,
    power_to           INTEGER,
    updated_at         TEXT NOT NULL,
    FOREIGN KEY (generation_id) REFERENCES generations(id),
    UNIQUE (generation_id, transmission, fuel_type)
)
"""

_CONFIG_COLUMNS = (
    "id, generation_id, transmission, fuel_type, engine_volume_from, "
    "engine_volume_to, power_from, power_to, updated_at"
)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Возвращает ``metadata.schema_version`` текущей базы.

    Args:
        conn: Открытое соединение SQLite.

    Returns:
        int: версия схемы; ``0``, если таблица ``metadata`` отсутствует
        или ключ не записан (совсем новая/повреждённая база).
    """
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def has_configurations_unique_index(conn: sqlite3.Connection) -> bool:
    """Проверяет наличие UNIQUE-индекса ``(generation_id, transmission, fuel_type)``.

    Работает и для таблиц, созданных через ``UNIQUE``-ограничение в
    ``CREATE TABLE`` (SQLite создаёт автоиндекс ``sqlite_autoindex_*``),
    и для явного ``CREATE UNIQUE INDEX``.

    Args:
        conn: Открытое соединение SQLite.

    Returns:
        bool: ``True``, если уникальный индекс уже есть (миграция не нужна).
    """
    for idx in conn.execute("PRAGMA index_list(configurations)").fetchall():
        # Строка: (seq, name, unique, origin, partial)
        if not idx[2]:
            continue
        columns = [row[2] for row in conn.execute(
            f"PRAGMA index_info({idx[1]})"
        ).fetchall()]
        if columns == ["generation_id", "transmission", "fuel_type"]:
            return True
    return False


def backup_database(db_path: Path, suffix: str = constants.DB_BACKUP_V1_SUFFIX) -> Path:
    """Создаёт резервную копию файла БД (обязательна перед ALTER TABLE).

    Копия пишется рядом с оригиналом: ``data/aprice.db`` -> ``data/aprice.db.bak-v1``.
    Если копия уже существует, она перезаписывается только что снятым слепком.

    Args:
        db_path: Путь к файлу ``aprice.db``.
        suffix: Суффикс имени копии (:data:`constants.DB_BACKUP_V1_SUFFIX`).

    Returns:
        Path: путь созданного файла копии.
    """
    backup_path = Path(str(db_path) + suffix)
    shutil.copy2(db_path, backup_path)
    logger.info("Резервная копия БД создана: %s", backup_path)
    return backup_path


def migrate_to_v2(conn: sqlite3.Connection, db_path: Path | None = None) -> bool:
    """Мигрирует схему ``configurations`` к версии 2 (UNIQUE-бизнес-ключ).

    Порядок действий:

    1. Если UNIQUE-индекса нет — создать резервную копию БД
       (``db.bak-v1``; при ``db_path=None`` копирование пропускается,
       например для соединения с базой в памяти из тестов).
    2. ``ALTER TABLE configurations RENAME TO configurations_old``.
    3. Создать новую таблицу с ``UNIQUE (generation_id, transmission, fuel_type)``.
    4. Перенести данные ``INSERT OR IGNORE ... SELECT`` (дубликаты схлопываются;
       при переносе сохраняется исходный ``id`` — внешние ключей на
       ``configurations`` в схеме нет, поэтому переписка безопасна).
    5. Удалить старую таблицу, пересоздать обычный индекс по ``generation_id``.
    6. Обновить ``metadata.schema_version`` до ``2``.

    Args:
        conn: Открытое соединение SQLite (блокировка на время миграции —
            ответственность вызывающего; Builder выполняет миграцию до
            старта фоновых потоков).
        db_path: Необязательный путь к файлу БД для резервного копирования.

    Returns:
        bool: ``True``, если миграция фактически выполнена; ``False``,
        если схема уже версии 2 (миграция не потребовалась).

    Raises:
        sqlite3.Error: при сбое SQL-операций (транзакция откатывается).
    """
    if has_configurations_unique_index(conn):
        version = get_schema_version(conn)
        if version < 2:
            _set_schema_version(conn, 2)
        logger.debug("Миграция v2 не требуется: UNIQUE-индекс уже присутствует")
        return False

    if db_path is not None and Path(db_path).exists():
        backup_database(Path(db_path))

    logger.info("Миграция схемы v1 -> v2: пересоздаю таблицу configurations")
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE configurations RENAME TO configurations_old")
        conn.execute(_CONFIGURATIONS_V2_SQL)
        conn.execute(
            f"INSERT OR IGNORE INTO configurations ({_CONFIG_COLUMNS}) "
            f"SELECT {_CONFIG_COLUMNS} FROM configurations_old ORDER BY id"
        )
        moved = conn.execute("SELECT COUNT(*) FROM configurations").fetchone()[0]
        dropped_old = conn.execute(
            "SELECT COUNT(*) FROM configurations_old"
        ).fetchone()[0]
        conn.execute("DROP TABLE configurations_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_configurations_gen_id "
            "ON configurations (generation_id)"
        )
        _set_schema_version(conn, 2)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        conn.rollback()
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    logger.info(
        "Миграция v2 завершена: перенесено строк %d (было %d, дубликаты схлопнуты)",
        moved,
        dropped_old,
    )
    return True


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Записывает ``metadata.schema_version`` (UPSERT)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "INSERT INTO metadata (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )
