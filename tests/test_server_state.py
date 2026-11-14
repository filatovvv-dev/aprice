"""Тесты хранилища состояния Server (подзадача 3.2).

Чистые юнит-тесты без сети и Qt: только stdlib sqlite3 через StateStore.
Все проверки используют tmp_path — рабочая data/aprice_server_state.db
не затрагивается.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from server.state import StateStore


@pytest.fixture()
def store(tmp_path):
    """Открывает StateStore на временном файле БД, закрывает после теста."""
    st = StateStore(tmp_path / "test_state.db")
    yield st
    st.close()


SAMPLE_SPEC = {
    "mode": "exact",
    "manufacturer": "BMW",
    "model": "X5",
    "generation": "IV (2018–2021)",
    "year_from": 2018,
    "year_to": 2021,
    "transmissions": ["АКПП"],
    "fuels": ["Бензин"],
}

SAMPLE_OPTIONS = {
    "sites": ["auto.ru", "drom.ru", "avito.ru"],
    "max_ads_per_site": 500,
    "max_pages_per_site": 10,
}


def _make_ad(url: str, site: str = "auto.ru", **overrides) -> dict:
    ad = {
        "url": url,
        "price": 3_500_000,
        "year": 2019,
        "manufacturer": "BMW",
        "model": "X5",
        "transmission": "АКПП",
        "fuel": "Бензин",
        "engine_volume": 3.0,
        "mileage": 60_000,
        "raw_json": {"title": f"Объявление {site}"},
    }
    ad.update(overrides)
    return ad


# ---------------------------------------------------------------------------
# 1. Создание схемы
# ---------------------------------------------------------------------------

def test_schema_created(tmp_path):
    """После инициализации таблицы tasks/collected_ads/server_meta существуют."""
    db = tmp_path / "schema.db"
    st = StateStore(db)
    try:
        names = {
            row[0]
            for row in st._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"tasks", "collected_ads", "server_meta"} <= names
        idx = {
            row[0]
            for row in st._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='idx_ads_task_site'"
            )
        }
        assert idx == {"idx_ads_task_site"}
    finally:
        st.close()


def test_foreign_keys_pragma_on(store):
    """ST-04: PRAGMA foreign_keys включён при открытии соединения."""
    cur = store._conn.execute("PRAGMA foreign_keys")
    assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 2. save_task + load_task round-trip
# ---------------------------------------------------------------------------

def test_save_load_task_roundtrip(store):
    """spec/options с вложенными структурами и кириллицей сохраняются точно."""
    store.save_task("t-1", SAMPLE_SPEC, SAMPLE_OPTIONS)
    task = store.load_task("t-1")
    assert task is not None
    assert task["task_id"] == "t-1"
    assert task["spec"] == SAMPLE_SPEC
    assert task["options"] == SAMPLE_OPTIONS
    assert task["state"] == "queued"
    assert task["started_at"] is None
    assert task["finished_at"] is None
    # created_at — UTC ISO 8601 (ST-09)
    dt = datetime.fromisoformat(task["created_at"])
    assert dt.tzinfo is not None


def test_load_task_missing_returns_none(store):
    """load_task для несуществующей задачи возвращает None."""
    assert store.load_task("nope") is None


# ---------------------------------------------------------------------------
# 3. Идемпотентность save_task (ST-07)
# ---------------------------------------------------------------------------

def test_save_task_idempotent(store):
    """Повторный save_task не создаёт дубликат и не меняет created_at/spec."""
    store.save_task("t-2", SAMPLE_SPEC, SAMPLE_OPTIONS)
    first = store.load_task("t-2")
    store.save_task("t-2", {"mode": "range"}, {"sites": []})  # новые spec не должны перезаписать
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_id = 't-2'"
    ).fetchone()[0]
    assert rows == 1
    second = store.load_task("t-2")
    assert second["created_at"] == first["created_at"]
    assert second["spec"] == SAMPLE_SPEC  # spec исходной записи сохранён
    assert second["options"] == SAMPLE_OPTIONS


def test_save_task_updates_state_only(store):
    """ON CONFLICT обновляет state переданным значением."""
    store.save_task("t-3", SAMPLE_SPEC, SAMPLE_OPTIONS, state="queued")
    store.save_task("t-3", SAMPLE_SPEC, SAMPLE_OPTIONS, state="running")
    assert store.load_task("t-3")["state"] == "running"


# ---------------------------------------------------------------------------
# 4. update_task_state: timestamps
# ---------------------------------------------------------------------------

def test_update_state_running_sets_started_at(store):
    """queued -> running проставляет started_at, finished_at остаётся NULL."""
    store.save_task("t-4", SAMPLE_SPEC, SAMPLE_OPTIONS)
    store.update_task_state("t-4", "running")
    task = store.load_task("t-4")
    assert task["state"] == "running"
    assert task["started_at"] is not None
    assert task["finished_at"] is None


def test_update_state_completed_sets_finished_at(store):
    """running -> completed проставляет finished_at; started_at сохраняется."""
    store.save_task("t-5", SAMPLE_SPEC, SAMPLE_OPTIONS)
    store.update_task_state("t-5", "running")
    started = store.load_task("t-5")["started_at"]
    store.update_task_state("t-5", "completed")
    task = store.load_task("t-5")
    assert task["state"] == "completed"
    assert task["finished_at"] is not None
    assert task["started_at"] == started  # COALESCE не перезатёр


def test_update_state_invalid_raises(store):
    """Недопустимое состояние -> ValueError."""
    store.save_task("t-6", SAMPLE_SPEC, SAMPLE_OPTIONS)
    with pytest.raises(ValueError):
        store.update_task_state("t-6", "teleporting")


# ---------------------------------------------------------------------------
# 5. list_active_tasks
# ---------------------------------------------------------------------------

def test_list_active_tasks_filters_and_orders(store):
    """Возвращаются только queued/running/stopping, порядок по created_at."""
    # Создаём задачи с искусственно разным created_at напрямую в БД
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    states = [
        ("a-running", "running", 1),
        ("b-queued", "queued", 2),
        ("c-completed", "completed", 3),
        ("d-stopping", "stopping", 4),
        ("e-failed", "failed", 5),
        ("e-cancelled", "cancelled", 6),
    ]
    for task_id, state, offset in states:
        store.save_task(task_id, SAMPLE_SPEC, SAMPLE_OPTIONS, state=state)
        ts = (base.replace(minute=offset)).isoformat(timespec="seconds")
        with store._conn:
            store._conn.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                (ts, task_id),
            )
    active = store.list_active_tasks()
    ids = [t["task_id"] for t in active]
    assert ids == ["a-running", "b-queued", "d-stopping"]


# ---------------------------------------------------------------------------
# 6. save_progress
# ---------------------------------------------------------------------------

def test_save_progress_roundtrip(store):
    """save_progress обновляет progress, видимый через load_task."""
    store.save_task("t-7", SAMPLE_SPEC, SAMPLE_OPTIONS)
    progress = {"site": "avito.ru", "page": 3, "total_pages": 10,
                "ads_collected": 45}
    store.save_progress("t-7", progress)
    assert store.load_task("t-7")["progress"] == progress


def test_save_progress_nonexistent_task_noop(store):
    """save_progress для несуществующей задачи — no-op, без падения."""
    store.save_progress("ghost", {"page": 1})
    assert store.load_task("ghost") is None


# ---------------------------------------------------------------------------
# 7. save_ads: батч + идемпотентность PK
# ---------------------------------------------------------------------------

def test_save_ads_count_and_dedup(store):
    """3 объявления сохраняются; повторная вставка с теми же url — 0 новых."""
    store.save_task("t-8", SAMPLE_SPEC, SAMPLE_OPTIONS)
    ads = [_make_ad(f"https://auto.ru/ad/{i}") for i in range(3)]
    inserted = store.save_ads("t-8", "auto.ru", ads)
    assert inserted == 3
    assert store.count_ads("t-8") == 3
    # повтор — те же url
    again = store.save_ads("t-8", "auto.ru", ads)
    assert again == 0
    assert store.count_ads("t-8") == 3


def test_save_ads_partial_overlap(store):
    """Из 2 объявлений (1 новый, 1 дубликат) вставляется ровно 1 строка."""
    store.save_task("t-8b", SAMPLE_SPEC, SAMPLE_OPTIONS)
    store.save_ads("t-8b", "auto.ru", [_make_ad("https://auto.ru/ad/1")])
    n = store.save_ads(
        "t-8b",
        "auto.ru",
        [_make_ad("https://auto.ru/ad/1"), _make_ad("https://auto.ru/ad/2")],
    )
    assert n == 1
    assert store.count_ads("t-8b") == 2


def test_save_ads_empty_list(store):
    """Пустой список объявлений — 0 вставок, без транзакционной ошибки."""
    assert store.save_ads("t-8c", "auto.ru", []) == 0


# ---------------------------------------------------------------------------
# 8. load_ads с фильтром по site
# ---------------------------------------------------------------------------

def test_load_ads_filter_by_site(store):
    """load_ads(site=...) возвращает только объявления этой площадки."""
    store.save_task("t-9", SAMPLE_SPEC, SAMPLE_OPTIONS)
    store.save_ads("t-9", "auto.ru", [_make_ad("https://auto.ru/a/1")])
    store.save_ads(
        "t-9",
        "drom.ru",
        [_make_ad("https://drom.ru/a/1", site="drom.ru"),
         _make_ad("https://drom.ru/a/2", site="drom.ru")],
    )
    all_ads = store.load_ads("t-9")
    assert len(all_ads) == 3
    drom_ads = store.load_ads("t-9", site="drom.ru")
    assert len(drom_ads) == 2
    assert all(a["site"] == "drom.ru" for a in drom_ads)
    assert store.count_ads("t-9", "auto.ru") == 1
    assert store.count_ads("t-9", "drom.ru") == 2


def test_load_ads_fields_roundtrip(store):
    """Поля объявления (цена, год, КПП, топливо, объём, пробег) сохраняются."""
    store.save_task("t-9b", SAMPLE_SPEC, SAMPLE_OPTIONS)
    ad = _make_ad("https://auto.ru/x/1")
    store.save_ads("t-9b", "auto.ru", [ad])
    loaded = store.load_ads("t-9b")[0]
    assert loaded["price"] == 3_500_000
    assert loaded["year"] == 2019
    assert loaded["transmission"] == "АКПП"
    assert loaded["fuel"] == "Бензин"
    assert loaded["engine_volume"] == 3.0
    assert loaded["mileage"] == 60_000
    # raw_json сериализуется в строку JSON
    assert json.loads(loaded["raw_json"])["title"] == "Объявление auto.ru"


# ---------------------------------------------------------------------------
# 9. delete_task: каскад FK (ST-04)
# ---------------------------------------------------------------------------

def test_delete_task_cascades_ads(store):
    """delete_task удаляет задачу и её объявления каскадно."""
    store.save_task("t-10", SAMPLE_SPEC, SAMPLE_OPTIONS)
    store.save_ads("t-10", "auto.ru", [_make_ad("https://auto.ru/d/1")])
    assert store.count_ads("t-10") == 1
    assert store.delete_task("t-10") is True
    assert store.load_task("t-10") is None
    assert store.count_ads("t-10") == 0


def test_delete_task_missing_returns_false(store):
    """Удаление несуществующей задачи возвращает False."""
    assert store.delete_task("ghost") is False


def test_fk_blocks_orphan_ads(store):
    """Вставка объявления для несуществующей задачи отклоняется FK."""
    with pytest.raises(sqlite3.IntegrityError):
        store.save_ads("no-such-task", "auto.ru", [_make_ad("https://x/1")])


# ---------------------------------------------------------------------------
# 10. set_meta / get_meta
# ---------------------------------------------------------------------------

def test_meta_roundtrip_and_upsert(store):
    """set_meta/get_meta: запись, чтение, обновление значения."""
    store.set_meta("last_shutdown_at", "2026-11-04T12:00:00+00:00")
    assert store.get_meta("last_shutdown_at") == "2026-11-04T12:00:00+00:00"
    store.set_meta("last_shutdown_at", "2026-11-05T08:30:00+00:00")
    assert store.get_meta("last_shutdown_at") == "2026-11-05T08:30:00+00:00"
    cnt = store._conn.execute("SELECT COUNT(*) FROM server_meta").fetchone()[0]
    assert cnt == 1


def test_get_meta_default_for_missing_key(store):
    """get_meta возвращает default для отсутствующего ключа."""
    assert store.get_meta("absent") is None
    assert store.get_meta("absent", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# 11. Объявление без url: пропуск + warning (caplog)
# ---------------------------------------------------------------------------

def test_save_ads_skips_url_less_with_warning(store, caplog):
    """Объявление без url пропускается, логируется warning, сбор не падает."""
    store.save_task("t-11", SAMPLE_SPEC, SAMPLE_OPTIONS)
    good = _make_ad("https://auto.ru/g/1")
    bad = {"price": 100, "year": 2020}  # нет url
    with caplog.at_level(logging.WARNING, logger="aprice.server.state"):
        inserted = store.save_ads("t-11", "auto.ru", [good, bad])
    assert inserted == 1
    assert store.count_ads("t-11") == 1
    assert any("без url" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 12. Персистентность: close + повторное открытие
# ---------------------------------------------------------------------------

def test_persistence_across_reopen(tmp_path):
    """После close() новый StateStore видит задачи, объявления и метаданные."""
    db = tmp_path / "persist.db"
    st1 = StateStore(db)
    st1.save_task("p-1", SAMPLE_SPEC, SAMPLE_OPTIONS)
    st1.update_task_state("p-1", "running")
    st1.save_progress("p-1", {"site": "auto.ru", "page": 2, "ads_collected": 10})
    st1.save_ads("p-1", "auto.ru", [_make_ad("https://auto.ru/p/1")])
    st1.set_meta("restored_task_id", "p-1")
    st1.close()

    st2 = StateStore(db)
    try:
        task = st2.load_task("p-1")
        assert task is not None
        assert task["state"] == "running"
        assert task["progress"]["page"] == 2
        assert st2.count_ads("p-1") == 1
        assert st2.get_meta("restored_task_id") == "p-1"
    finally:
        st2.close()


# ---------------------------------------------------------------------------
# Доп.: контекстный менеджер и дефолтный путь из constants
# ---------------------------------------------------------------------------

def test_context_manager_closes(store, tmp_path):
    """with StateStore(...) as st: ... — соединение закрыто на выходе."""
    db = tmp_path / "ctx.db"
    with StateStore(db) as st:
        st.save_task("ctx-1", SAMPLE_SPEC, SAMPLE_OPTIONS)
    with pytest.raises(sqlite3.ProgrammingError):
        st._conn.execute("SELECT 1")


def test_default_db_path_uses_constant(tmp_path, monkeypatch):
    """ST-05: без db_path используется SERVER_STATE_DB из common.constants."""
    from common import constants

    fake = tmp_path / "default_state.db"
    monkeypatch.setattr(constants, "SERVER_STATE_DB", fake)
    # патчим ссылку, импортированную в модуле state
    import server.state as state_mod

    monkeypatch.setattr(state_mod, "SERVER_STATE_DB", fake)
    st = StateStore()
    try:
        assert st.db_path == fake
        assert fake.exists()
    finally:
        st.close()
