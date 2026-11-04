"""Хранилище состояния APrice-Server (подзадача 3.2).

Класс :class:`StateStore` — «глупое» SQLite-хранилище без логики очереди,
пауз и ETA (это задача 3.4). Отвечает за:

* схему БД ``data/aprice_server_state.db`` (таблицы ``tasks``,
  ``collected_ads``, ``server_meta``);
* CRUD задач (сохранение/загрузка/смена состояния/прогресс/удаление);
* батч-вставку собранных объявлений с дедупликацией по PK;
* пары ключ-значение для метаданных сервера.

Требования: ST-01..ST-09 (только stdlib, параметризованный SQL,
транзакции через контекстный менеджер, FK-каскад, UTC ISO 8601).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from common.constants import SERVER_STATE_DB

logger = logging.getLogger("aprice.server.state")

# Состояния задачи, при которых она считается активной (для восстановления).
ACTIVE_STATES: tuple[str, ...] = ("queued", "running", "stopping")

# Все допустимые состояния FSM задачи (валидация update_task_state).
VALID_STATES: frozenset[str] = frozenset(
    {"queued", "running", "stopping", "completed", "failed", "cancelled"}
)

# Конечные состояния — проставляется finished_at.
FINAL_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)

_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    spec_json      TEXT NOT NULL,
    options_json   TEXT NOT NULL,
    state          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    progress_json  TEXT
);

CREATE TABLE IF NOT EXISTS collected_ads (
    task_id         TEXT NOT NULL,
    site            TEXT NOT NULL,
    url             TEXT NOT NULL,
    price           INTEGER,
    year            INTEGER,
    manufacturer    TEXT,
    model           TEXT,
    transmission    TEXT,
    fuel            TEXT,
    engine_volume   REAL,
    mileage         INTEGER,
    raw_json        TEXT,
    collected_at    TEXT NOT NULL,
    PRIMARY KEY (task_id, site, url),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS server_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_ads_task_site
    ON collected_ads(task_id, site);
"""


def _utc_now_iso() -> str:
    """Возвращает текущее время в UTC, формат ISO 8601 (ST-09).

    Returns:
        str: например ``2026-11-04T12:00:00+00:00``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """SQLite-хранилище состояния Server (задачи, объявления, метаданные).

    Используется из GUI-потока Server (3.4–3.6); многопоточный доступ не
    поддерживается (``check_same_thread`` намеренно оставлен ``True``).

    Attributes:
        db_path: путь к файлу БД.
    """

    def __init__(self, db_path: Optional[Union[Path, str]] = None) -> None:
        """Открывает (при необходимости создаёт) БД и инициализирует схему.

        Args:
            db_path: путь к файлу БД; по умолчанию —
                ``common.constants.SERVER_STATE_DB``
                (``data/aprice_server_state.db``).
        """
        self.db_path: Path = Path(db_path) if db_path is not None else SERVER_STATE_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # ST-01: только stdlib sqlite3; никаких ORM.
        self._conn: sqlite3.Connection = sqlite3.connect(str(self.db_path))
        # Включаем внешние ключи: без PRAGMA ON DELETE CASCADE не работает (ST-04).
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        logger.info("StateStore открыт: %s", self.db_path)

    # ------------------------------------------------------------------
    # Схема
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        """Создаёт таблицы и индексы, если их ещё нет (идемпотентно)."""
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ------------------------------------------------------------------
    # Задачи
    # ------------------------------------------------------------------
    def save_task(
        self,
        task_id: str,
        spec: dict[str, Any],
        options: dict[str, Any],
        state: str = "queued",
        progress: Optional[dict[str, Any]] = None,
    ) -> None:
        """Сохраняет задачу; повторный вызов не создаёт дубликат (ST-07).

        При конфликте по ``task_id`` обновляются только ``state`` и
        ``progress_json``; ``spec``/``options``/``created_at`` исходной
        записи сохраняются неизменными.

        Args:
            task_id: идентификатор задачи (UUID v4).
            spec: спецификация поиска (словарь, сериализуется в JSON).
            options: параметры сбора (словарь, сериализуется в JSON).
            state: начальное/актуальное состояние FSM.
            progress: опциональный прогресс ``{"site": ..., "page": ...}``.
        """
        now = _utc_now_iso()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO tasks (task_id, spec_json, options_json, state,
                                   created_at, progress_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    progress_json = COALESCE(excluded.progress_json,
                                             tasks.progress_json)
                """,
                (
                    task_id,
                    json.dumps(spec, ensure_ascii=False),
                    json.dumps(options, ensure_ascii=False),
                    state,
                    now,
                    json.dumps(progress, ensure_ascii=False)
                    if progress is not None
                    else None,
                ),
            )
        logger.debug("save_task: %s (state=%s)", task_id, state)

    def load_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Загружает задачу по идентификатору.

        Args:
            task_id: идентификатор задачи.

        Returns:
            dict | None: ``{task_id, spec, options, state, created_at,
            started_at, finished_at, progress}`` или ``None``, если задачи
            нет в БД.
        """
        row = self._conn.execute(
            "SELECT task_id, spec_json, options_json, state, created_at,"
            " started_at, finished_at, progress_json"
            " FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    @staticmethod
    def _row_to_task(row: tuple[Any, ...]) -> dict[str, Any]:
        """Преобразует кортеж строки таблицы ``tasks`` в словарь.

        Args:
            row: кортеж ``(task_id, spec_json, options_json, state,
                created_at, started_at, finished_at, progress_json)``.

        Returns:
            dict: задача с десериализованными JSON-полями.
        """
        (
            task_id,
            spec_json,
            options_json,
            state,
            created_at,
            started_at,
            finished_at,
            progress_json,
        ) = row
        return {
            "task_id": task_id,
            "spec": json.loads(spec_json) if spec_json else {},
            "options": json.loads(options_json) if options_json else {},
            "state": state,
            "created_at": created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "progress": json.loads(progress_json) if progress_json else None,
        }

    def list_active_tasks(self) -> list[dict[str, Any]]:
        """Возвращает незавершённые задачи для восстановления (3.6).

        Активными считаются состояния ``queued``, ``running``, ``stopping``.
        Порядок — по ``created_at`` (старые первыми, ST-08).

        Returns:
            list[dict]: список задач в формате :meth:`load_task`.
        """
        # Плейсхолдеры формируются из фиксированного кортежа ACTIVE_STATES
        # (не из пользовательских данных); все значения — параметризованы (?).
        placeholders = ", ".join(["?"] * len(ACTIVE_STATES))
        sql = (
            "SELECT task_id, spec_json, options_json, state, created_at,"
            " started_at, finished_at, progress_json"
            " FROM tasks WHERE state IN (" + placeholders + ")"
            " ORDER BY created_at ASC"
        )
        rows = self._conn.execute(sql, ACTIVE_STATES).fetchall()
        return [self._row_to_task(row) for row in rows]

    def update_task_state(self, task_id: str, state: str) -> None:
        """Меняет состояние задачи; проставляет служебные timestamps.

        При переходе в ``running`` заполняется ``started_at`` (если ещё не
        заполнен); при переходе в конечное состояние
        (``completed``/``failed``/``cancelled``) — ``finished_at``.

        Args:
            task_id: идентификатор задачи.
            state: новое состояние из ``VALID_STATES``.

        Raises:
            ValueError: если ``state`` не входит в допустимый набор FSM.
        """
        if state not in VALID_STATES:
            raise ValueError(f"Недопустимое состояние задачи: {state!r}")
        now = _utc_now_iso()
        with self._conn:
            if state == "running":
                self._conn.execute(
                    "UPDATE tasks SET state = ?,"
                    " started_at = COALESCE(started_at, ?)"
                    " WHERE task_id = ?",
                    (state, now, task_id),
                )
            elif state in FINAL_STATES:
                self._conn.execute(
                    "UPDATE tasks SET state = ?, finished_at = ?"
                    " WHERE task_id = ?",
                    (state, now, task_id),
                )
            else:
                self._conn.execute(
                    "UPDATE tasks SET state = ? WHERE task_id = ?",
                    (state, task_id),
                )
        logger.debug("update_task_state: %s -> %s", task_id, state)

    def save_progress(self, task_id: str, progress: dict[str, Any]) -> None:
        """Сохраняет прогресс задачи (вызывается после каждой страницы, S-06).

        Лёгкий метод: один параметризованный ``UPDATE`` без чтений.

        Args:
            task_id: идентификатор задачи.
            progress: словарь вида
                ``{"site": "avito.ru", "page": 3, "ads_collected": 45}``.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET progress_json = ? WHERE task_id = ?",
                (json.dumps(progress, ensure_ascii=False), task_id),
            )

    def delete_task(self, task_id: str) -> bool:
        """Удаляет задачу и каскадно — её объявления (для cancel_task).

        Args:
            task_id: идентификатор задачи.

        Returns:
            bool: ``True``, если задача существовала и удалена.
        """
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM tasks WHERE task_id = ?", (task_id,)
            )
        deleted = cur.rowcount > 0
        logger.debug("delete_task: %s (deleted=%s)", task_id, deleted)
        return deleted

    # ------------------------------------------------------------------
    # Объявления
    # ------------------------------------------------------------------
    def save_ads(self, task_id: str, site: str, ads: list[dict[str, Any]]) -> int:
        """Батч-вставка собранных объявлений одной площадки (ST-07).

        Дедупликация по первичному ключу ``(task_id, site, url)``:
        ``INSERT OR IGNORE``. Объявления без поля ``url`` пропускаются с
        логом ``warning`` (не роняют сбор).

        Args:
            task_id: идентификатор задачи.
            site: площадка (например ``"auto.ru"``).
            ads: список словарей-объявлений; обязательное поле — ``url``.

        Returns:
            int: количество фактически вставленных строк.
        """
        if not ads:
            return 0
        now = _utc_now_iso()
        rows: list[tuple[Any, ...]] = []
        skipped = 0
        for ad in ads:
            url = ad.get("url")
            if not url:
                skipped += 1
                logger.warning(
                    "save_ads: объявление без url пропущено"
                    " (task=%s, site=%s): %r",
                    task_id,
                    site,
                    ad,
                )
                continue
            raw = ad.get("raw_json")
            if isinstance(raw, (dict, list)):
                raw = json.dumps(raw, ensure_ascii=False)
            rows.append(
                (
                    task_id,
                    site,
                    str(url),
                    ad.get("price"),
                    ad.get("year"),
                    ad.get("manufacturer"),
                    ad.get("model"),
                    ad.get("transmission"),
                    ad.get("fuel"),
                    ad.get("engine_volume"),
                    ad.get("mileage"),
                    raw,
                    now,
                )
            )
        if not rows:
            return 0
        inserted = 0
        with self._conn:
            cur = self._conn.executemany(
                """
                INSERT OR IGNORE INTO collected_ads (
                    task_id, site, url, price, year, manufacturer, model,
                    transmission, fuel, engine_volume, mileage, raw_json,
                    collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            # sqlite3: cur.rowcount для executemany — число реально вставленных
            # строк (OR IGNORE-конфликты не учитываются).
            inserted = cur.rowcount if cur.rowcount >= 0 else len(rows)
        if skipped:
            logger.warning(
                "save_ads: task=%s site=%s — пропущено %d объявл. без url",
                task_id,
                site,
                skipped,
            )
        logger.debug(
            "save_ads: task=%s site=%s — вставлено %d/%d",
            task_id,
            site,
            inserted,
            len(ads),
        )
        return inserted

    def load_ads(self, task_id: str, site: Optional[str] = None) -> list[dict[str, Any]]:
        """Загружает объявления задачи (опционально — одной площадки).

        Args:
            task_id: идентификатор задачи.
            site: название площадки или ``None`` (все площадки).

        Returns:
            list[dict]: объявления в формате таблицы ``collected_ads``.
        """
        if site is None:
            rows = self._conn.execute(
                "SELECT site, url, price, year, manufacturer, model,"
                " transmission, fuel, engine_volume, mileage, raw_json,"
                " collected_at"
                " FROM collected_ads WHERE task_id = ?"
                " ORDER BY site, collected_at, url",
                (task_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT site, url, price, year, manufacturer, model,"
                " transmission, fuel, engine_volume, mileage, raw_json,"
                " collected_at"
                " FROM collected_ads WHERE task_id = ? AND site = ?"
                " ORDER BY collected_at, url",
                (task_id, site),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            (
                r_site,
                url,
                price,
                year,
                manufacturer,
                model,
                transmission,
                fuel,
                engine_volume,
                mileage,
                raw_json,
                collected_at,
            ) = row
            result.append(
                {
                    "site": r_site,
                    "url": url,
                    "price": price,
                    "year": year,
                    "manufacturer": manufacturer,
                    "model": model,
                    "transmission": transmission,
                    "fuel": fuel,
                    "engine_volume": engine_volume,
                    "mileage": mileage,
                    "raw_json": raw_json,
                    "collected_at": collected_at,
                }
            )
        return result

    def count_ads(self, task_id: str, site: Optional[str] = None) -> int:
        """Считает количество сохранённых объявлений задачи.

        Args:
            task_id: идентификатор задачи.
            site: название площадки или ``None`` (все площадки).

        Returns:
            int: число объявлений.
        """
        if site is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM collected_ads WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM collected_ads"
                " WHERE task_id = ? AND site = ?",
                (task_id, site),
            ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        """Сохраняет пару ключ-значение (UPSERT).

        Примеры ключей: ``last_shutdown_at``, ``restored_task_id``.

        Args:
            key: имя метаданного.
            value: значение (строка).
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO server_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Читает метаданное по ключу.

        Args:
            key: имя метаданного.
            default: значение, возвращаемое при отсутствии ключа.

        Returns:
            str | None: значение или ``default``.
        """
        row = self._conn.execute(
            "SELECT value FROM server_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Закрывает соединение с БД."""
        try:
            self._conn.close()
            logger.info("StateStore закрыт: %s", self.db_path)
        except sqlite3.Error as exc:  # pragma: no cover - защита при завершении
            logger.error("Ошибка при закрытии StateStore: %s", exc)

    def __enter__(self) -> "StateStore":
        """Контекстный менеджер: возвращает сам хранилище."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Контекстный менеджер: закрывает соединение."""
        self.close()
