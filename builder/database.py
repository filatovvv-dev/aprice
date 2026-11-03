"""Слой доступа к SQLite-базе справочников APrice (``data/aprice.db``).

Реализует:

* создание схемы при первом запуске;
* UPSERT-записи (``INSERT ... ON CONFLICT ... DO UPDATE``) — требование P-09:
  повторный запуск обновляет ``updated_at``, а не дублирует строки;
* агрегированные счётчики для строки статуса GUI.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from common import constants

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS manufacturers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    source_id   TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL,
    name            TEXT NOT NULL,
    source_id       TEXT,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (manufacturer_id) REFERENCES manufacturers(id),
    UNIQUE (manufacturer_id, name)
);

CREATE TABLE IF NOT EXISTS generations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    year_from   INTEGER,
    year_to     INTEGER,
    source_id   TEXT,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES models(id),
    UNIQUE (model_id, name)
);

CREATE TABLE IF NOT EXISTS configurations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id      INTEGER NOT NULL,
    transmission       TEXT,
    fuel_type          TEXT,
    engine_volume_from REAL,
    engine_volume_to   REAL,
    power_from         INTEGER,
    power_to           INTEGER,
    updated_at         TEXT NOT NULL,
    FOREIGN KEY (generation_id) REFERENCES generations(id)
);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_models_manufacturer_id ON models (manufacturer_id);
CREATE INDEX IF NOT EXISTS idx_generations_model_id   ON generations (model_id);
CREATE INDEX IF NOT EXISTS idx_configurations_gen_id  ON configurations (generation_id);
"""


def utc_now_iso() -> str:
    """Возвращает текущее время в формате ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Обёртка над SQLite-базой справочников автомобилей."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        """Инициализирует подключение.

        Args:
            db_path: Путь к файлу БД; по умолчанию :data:`constants.DB_FILE`.
        """
        self._path: Path = db_path or constants.DB_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self._path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        logger.debug("Открыто соединение с БД: %s", self._path)

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        """Создаёт таблицы и индексы (IF NOT EXISTS), проставляет metadata."""
        self._conn.executescript(_SCHEMA_SQL)
        self.set_metadata("schema_version", constants.SCHEMA_VERSION)
        if self.get_metadata("source") is None:
            self.set_metadata("source", "auto.ru/cars-base.ru")
        self._conn.commit()
        logger.info("Схема БД проверена/создана: %s", self._path)

    def close(self) -> None:
        """Закрывает соединение с базой."""
        self._conn.close()

    def __enter__(self) -> "Database":
        """Контекстный вход."""
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Контекстный выход — закрывает соединение."""
        self.close()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    def set_metadata(self, key: str, value: str) -> None:
        """Записывает/обновляет пару ключ-значение в таблице ``metadata``."""
        self._conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        """Возвращает значение metadata-ключа или ``None``."""
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def mark_update_finished(self) -> None:
        """Фиксирует в metadata дату-время успешного завершения обновления."""
        self.set_metadata("last_update", utc_now_iso())

    # ------------------------------------------------------------------
    # UPSERT сущностей (P-09)
    # ------------------------------------------------------------------
    def upsert_manufacturer(self, name: str, source_id: Optional[str] = None) -> int:
        """Добавляет марку или обновляет ``updated_at`` существующей.

        Returns:
            int: ``id`` записи марки.
        """
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO manufacturers (name, source_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE
                SET source_id  = COALESCE(excluded.source_id, manufacturers.source_id),
                    updated_at = excluded.updated_at
            """,
            (name, source_id, now),
        )
        row = self._conn.execute(
            "SELECT id FROM manufacturers WHERE name = ?", (name,)
        ).fetchone()
        return int(row["id"])

    def upsert_model(
        self, manufacturer_id: int, name: str, source_id: Optional[str] = None
    ) -> int:
        """Добавляет модель марки или обновляет ``updated_at`` существующей.

        Returns:
            int: ``id`` записи модели.
        """
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO models (manufacturer_id, name, source_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(manufacturer_id, name) DO UPDATE
                SET source_id  = COALESCE(excluded.source_id, models.source_id),
                    updated_at = excluded.updated_at
            """,
            (manufacturer_id, name, source_id, now),
        )
        row = self._conn.execute(
            "SELECT id FROM models WHERE manufacturer_id = ? AND name = ?",
            (manufacturer_id, name),
        ).fetchone()
        return int(row["id"])

    def upsert_generation(
        self,
        model_id: int,
        name: str,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        source_id: Optional[str] = None,
    ) -> int:
        """Добавляет поколение/рестайлинг либо обновляет существующий.

        Returns:
            int: ``id`` записи поколения.
        """
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO generations
                (model_id, name, year_from, year_to, source_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_id, name) DO UPDATE
                SET year_from  = COALESCE(excluded.year_from, generations.year_from),
                    year_to    = COALESCE(excluded.year_to, generations.year_to),
                    source_id  = COALESCE(excluded.source_id, generations.source_id),
                    updated_at = excluded.updated_at
            """,
            (model_id, name, year_from, year_to, source_id, now),
        )
        row = self._conn.execute(
            "SELECT id FROM generations WHERE model_id = ? AND name = ?",
            (model_id, name),
        ).fetchone()
        return int(row["id"])

    def upsert_configuration(
        self,
        generation_id: int,
        transmission: Optional[str],
        fuel_type: Optional[str],
        engine_volume_from: Optional[float] = None,
        engine_volume_to: Optional[float] = None,
        power_from: Optional[int] = None,
        power_to: Optional[int] = None,
    ) -> None:
        """Добавляет/обновляет конфигурацию (КПП/топливо/объём/мощность).

        Конфигурации не имеют уникального бизнес-ключа в схеме ТЗ, поэтому
        дедупликация выполняется по кортежу всех значений (частичный индекс
        реализуется через SELECT перед INSERT).
        """
        now = utc_now_iso()
        existing = self._conn.execute(
            """
            SELECT id FROM configurations
            WHERE generation_id IS ? AND transmission IS ? AND fuel_type IS ?
              AND engine_volume_from IS ? AND engine_volume_to IS ?
              AND power_from IS ? AND power_to IS ?
            LIMIT 1
            """,
            (
                generation_id,
                transmission,
                fuel_type,
                engine_volume_from,
                engine_volume_to,
                power_from,
                power_to,
            ),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE configurations SET updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO configurations
                    (generation_id, transmission, fuel_type,
                     engine_volume_from, engine_volume_to,
                     power_from, power_to, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    transmission,
                    fuel_type,
                    engine_volume_from,
                    engine_volume_to,
                    power_from,
                    power_to,
                    now,
                ),
            )

    def commit(self) -> None:
        """Фиксирует текущую транзакцию."""
        self._conn.commit()

    # ------------------------------------------------------------------
    # Статистика (для строки статуса GUI)
    # ------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """Возвращает количество записей по таблицам.

        Returns:
            dict: ``{'manufacturers': N, 'models': N, 'generations': N,
            'configurations': N}``.
        """
        result: dict[str, int] = {}
        for table in ("manufacturers", "models", "generations", "configurations"):
            row = self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            result[table] = int(row["c"])
        return result
