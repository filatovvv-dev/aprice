"""Интеграционные тесты TaskManager (подзадача 3.4b).

Проверяют связку ``TaskManager`` ↔ парсеры-заглушки (3.3) ↔ ``StateStore``
(3.2): сохранение объявлений и прогресса после каждой страницы (S-06),
изоляцию ошибок площадок (CAPTCHA / блокировка / неподдерживаемая — S-13,
S-14, S-15), восстановление после перезапуска (S-07) и правило «в БД пишет
только поток менеджера».

Qt — только ``QCoreApplication``; цикл событий «качается» через
:func:`pump_until`. Тесты не требуют сети.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import pytest
from PyQt6.QtCore import QCoreApplication

from common.config import Config
from server.parsers import AutoRuParser, DromParser
from server.parsers.base import BlockedError, CaptchaDetectedError
from server.state import StateStore
from server.task_manager import MAX_ADS_PER_PAGE_ESTIMATE, TaskManager

SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
}
FOUR = ["auto.ru", "drom.ru", "avito.ru", "bibika.ru"]
THREE = ["auto.ru", "drom.ru", "avito.ru"]


# ----------------------------------------------------------------------
# Инфраструктура
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    """Один QCoreApplication на модуль."""
    return QCoreApplication.instance() or QCoreApplication([])


def pump_until(cond: Callable[[], bool], timeout: float = 15.0) -> None:
    """Крутит цикл событий, пока ``cond()`` не станет истинным."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if cond():
            return
        time.sleep(0.005)
    QCoreApplication.processEvents()
    assert cond(), "pump_until: условие не выполнено за отведённое время"


class Recorder:
    """Собирает публичные сигналы TaskManager в порядке прихода."""

    def __init__(self, mgr: TaskManager) -> None:
        self.events: list[tuple] = []
        self.started_at: dict[str, float] = {}
        mgr.task_started.connect(self._started)
        mgr.task_progress.connect(
            lambda t, p: self.events.append(("progress", t, dict(p)))
        )
        mgr.task_completed.connect(lambda t, n: self.events.append(("completed", t, n)))
        mgr.task_stopped.connect(lambda t, n: self.events.append(("stopped", t, n)))
        mgr.task_failed.connect(lambda t, r: self.events.append(("failed", t, r)))
        mgr.captcha_required.connect(
            lambda t, s, u: self.events.append(("captcha", t, s, u))
        )

    def _started(self, task_id: str) -> None:
        self.started_at[task_id] = time.monotonic()
        self.events.append(("started", task_id))

    def of(self, kind: str, task_id: str | None = None) -> list[tuple]:
        return [
            e for e in self.events
            if e[0] == kind and (task_id is None or e[1] == task_id)
        ]

    def finished(self, task_id: str) -> bool:
        return any(
            self.of(k, task_id) for k in ("completed", "stopped", "failed")
        )


class RecordingStore(StateStore):
    """StateStore, запоминающий поток каждого вызова записи."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.write_threads: dict[str, set[int]] = {}
        self.progress_calls: list[dict] = []

    def _note(self, name: str) -> None:
        self.write_threads.setdefault(name, set()).add(threading.get_ident())

    def save_ads(self, task_id, site, ads):  # noqa: ANN001
        self._note("save_ads")
        return super().save_ads(task_id, site, ads)

    def save_progress(self, task_id, progress):  # noqa: ANN001
        self._note("save_progress")
        self.progress_calls.append(dict(progress))
        return super().save_progress(task_id, progress)

    def update_task_state(self, task_id, state):  # noqa: ANN001
        self._note("update_task_state")
        return super().update_task_state(task_id, state)


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config(tmp_path / "settings.json")


@pytest.fixture()
def store(tmp_path) -> RecordingStore:
    s = RecordingStore(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture()
def mgr(qapp, config, store):
    m = TaskManager(config, store)
    yield m
    m.shutdown()


def fast(sites=None, pages=3, delay=(0.05, 0.05), **extra) -> dict:
    """Опции быстрого режима с фиксированным ``test_delay``."""
    opts = {
        "sites": list(sites or THREE),
        "max_pages_per_site": pages,
        "test_fast_mode": True,
        "test_delay": list(delay),
    }
    opts.update(extra)
    return opts


def run_to_end(rec: Recorder, task_id: str, timeout: float = 20.0) -> None:
    pump_until(lambda: rec.finished(task_id), timeout)


def error_kinds(mgr: TaskManager, task_id: str) -> set[tuple[str, str]]:
    """Множество пар ``(site, kind)`` из ``Task.errors``."""
    return {(e["site"], e["kind"]) for e in mgr.get_task(task_id).errors if "site" in e}


def seed_task(store: StateStore, state: str, options: dict, progress=None) -> str:
    """Создаёт в БД задание «как после падения процесса»."""
    task_id = f"restored-{state}-{time.monotonic_ns()}"
    store.save_task(task_id, SPEC, options, state)
    if progress is not None:
        store.save_progress(task_id, progress)
    return task_id


# ----------------------------------------------------------------------
# 1–2. Интеграция с парсерами и StateStore
# ----------------------------------------------------------------------
def test_integration_ads_saved_in_state_store(mgr, store):
    """Реальные заглушки -> объявления лежат в StateStore по каждой площадке."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(sites=FOUR, pages=2))
    run_to_end(rec, task_id)
    assert rec.of("completed", task_id)

    ads = store.load_ads(task_id)
    assert ads, "объявления не сохранены"
    task = mgr.get_task(task_id)
    for site in FOUR:
        saved = store.load_ads(task_id, site)
        assert len(saved) == task.per_site[site]["ads"] == store.count_ads(task_id, site)
        assert all(a["url"].startswith(f"https://{site}/") for a in saved)
    assert rec.of("completed", task_id)[0][2] == len(ads) == task.total_ads


def test_progress_saved_after_every_page(mgr, store):
    """Прогресс пишется после каждой страницы и содержит page/ads_collected."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=3))
    run_to_end(rec, task_id)

    assert len(store.progress_calls) == len(THREE) * 3  # по одному на страницу
    assert [c["page"] for c in store.progress_calls] == [1, 1, 1, 2, 2, 2, 3, 3, 3]
    totals = [c["ads_collected"] for c in store.progress_calls]
    assert totals == sorted(totals) and totals[0] > 0

    progress = store.load_task(task_id)["progress"]
    assert progress["page"] == 3 and progress["site"] == "avito.ru"
    assert progress["ads_collected"] == store.count_ads(task_id)
    assert {s: v["pages_done"] for s, v in progress["per_site"].items()} == {
        s: 3 for s in THREE
    }


# ----------------------------------------------------------------------
# 3–6. Ошибки площадок
# ----------------------------------------------------------------------
def test_captcha_pauses_only_that_site(mgr, store):
    """CAPTCHA: captcha_required + errors; остальные площадки продолжают."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(simulate_captcha="auto.ru"))
    run_to_end(rec, task_id)

    captchas = rec.of("captcha", task_id)
    assert len(captchas) == 1
    assert captchas[0][2] == "auto.ru" and captchas[0][3].startswith("https://auto.ru/")
    assert ("auto.ru", "captcha") in error_kinds(mgr, task_id)

    task = mgr.get_task(task_id)
    assert "auto.ru" not in task.per_site or task.per_site["auto.ru"]["ads"] == 0
    for site in ("drom.ru", "avito.ru"):
        assert task.per_site[site]["pages_done"] == 3
    assert rec.of("completed", task_id) and not rec.of("failed", task_id)
    assert store.count_ads(task_id, "auto.ru") == 0
    assert store.count_ads(task_id, "drom.ru") > 0


def test_blocked_site_is_logged_and_skipped(mgr, store):
    """Блокировка: errors содержит blocked, задание продолжается."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(simulate_block="avito.ru"))
    run_to_end(rec, task_id)

    assert ("avito.ru", "blocked") in error_kinds(mgr, task_id)
    task = mgr.get_task(task_id)
    for site in ("auto.ru", "drom.ru"):
        assert task.per_site[site]["pages_done"] == 3
    assert rec.of("completed", task_id)
    assert not rec.of("captcha", task_id)


def test_unsupported_site_recorded(mgr):
    """sberauto.com: errors содержит unsupported, задание завершается.

    Единственная площадка не поддерживается -> «все площадки упали» ->
    итоговое состояние ``failed`` (см. следующий тест и допущения 3.4b).
    """
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(sites=["sberauto.com"]))
    run_to_end(rec, task_id)
    assert error_kinds(mgr, task_id) == {("sberauto.com", "unsupported")}
    assert rec.finished(task_id)
    assert mgr.get_task(task_id).total_ads == 0


def test_all_sites_failed_marks_task_failed(mgr, store):
    """Все площадки упали -> task_failed, состояние failed."""
    rec = Recorder(mgr)
    task_id = mgr.submit(
        SPEC, fast(sites=["sberauto.com"], simulate_captcha="sberauto.com")
    )
    run_to_end(rec, task_id)
    failed = rec.of("failed", task_id)
    assert len(failed) == 1 and "sberauto.com" in failed[0][2]
    assert not rec.of("completed", task_id)
    assert mgr.get_status(task_id)["state"] == "failed"
    assert store.load_task(task_id)["state"] == "failed"


def test_mixed_failures_all_down_is_failed(mgr):
    """CAPTCHA на единственной поддерживаемой + unsupported -> failed + captcha."""
    rec = Recorder(mgr)
    task_id = mgr.submit(
        SPEC, fast(sites=["auto.ru", "sberauto.com"], simulate_captcha="auto.ru")
    )
    run_to_end(rec, task_id)
    assert rec.of("captcha", task_id)
    assert error_kinds(mgr, task_id) == {
        ("auto.ru", "captcha"),
        ("sberauto.com", "unsupported"),
    }
    assert rec.of("failed", task_id)


def test_unexpected_parser_error_isolated(mgr, monkeypatch):
    """Произвольное исключение парсера -> kind=error, остальные продолжают."""

    def boom(self, spec, options, page, stop_event):  # noqa: ANN001
        raise ValueError("сломалась вёрстка")

    monkeypatch.setattr(DromParser, "fetch_page", boom)
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast())
    run_to_end(rec, task_id)
    assert ("drom.ru", "error") in error_kinds(mgr, task_id)
    task = mgr.get_task(task_id)
    assert task.per_site["auto.ru"]["pages_done"] == 3
    assert rec.of("completed", task_id)


# ----------------------------------------------------------------------
# 7–8. Восстановление после перезапуска
# ----------------------------------------------------------------------
def test_restore_running_task_continues_from_last_page(mgr, store):
    """running + progress(page=3) -> в очередь, per_site[auto.ru]=3, сбор с 4-й."""
    task_id = seed_task(
        store, "running", fast(sites=FOUR, pages=5, delay=(0.05, 0.05)),
        {"site": "auto.ru", "page": 3, "ads_collected": 60},
    )
    rec = Recorder(mgr)
    assert mgr.restore() == [task_id]
    assert mgr.queue_length() == 1
    task = mgr.get_task(task_id)
    assert task.per_site["auto.ru"]["pages_done"] == 3
    # Порядок round-robin: остальные площадки успели лишь до 2-й страницы.
    assert task.per_site["drom.ru"]["pages_done"] == 2
    assert mgr.get_status(task_id)["state"] == "queued"
    assert store.load_task(task_id)["state"] == "queued"

    run_to_end(rec, task_id)
    assert rec.of("completed", task_id)
    first_page = {}
    for e in rec.of("progress", task_id):
        first_page.setdefault(e[2]["site"], e[2]["page"])
    assert first_page == {"auto.ru": 4, "drom.ru": 3, "avito.ru": 3, "bibika.ru": 3}
    assert {v["pages_done"] for v in task.per_site.values()} == {5}


def test_restore_stopping_task_becomes_cancelled(mgr, store):
    """stopping -> сразу cancelled и task_stopped, в очередь не попадает."""
    task_id = seed_task(store, "stopping", fast())
    rec = Recorder(mgr)
    assert mgr.restore() == []
    assert mgr.queue_length() == 0
    assert rec.of("stopped", task_id) == [("stopped", task_id, 0)]
    assert mgr.get_status(task_id)["state"] == "cancelled"
    assert store.load_task(task_id)["state"] == "cancelled"
    assert store.list_active_tasks() == []


def test_restore_queued_task_runs(mgr, store):
    """queued в БД -> в очередь и выполняется; повторный restore не дублирует."""
    task_id = seed_task(store, "queued", fast(pages=2))
    rec = Recorder(mgr)
    assert mgr.restore() == [task_id]
    assert mgr.restore() == []  # уже известно менеджеру
    run_to_end(rec, task_id)
    assert rec.of("completed", task_id)
    assert not mgr.get_task(task_id).restored  # queued не считается «возвращением»


def test_restored_task_waits_before_first_request(mgr, store):
    """После перезапуска перед первым запросом — «человеческая» пауза."""
    delay = 0.4
    task_id = seed_task(
        store, "running", fast(pages=2, delay=(delay, delay)),
        {"site": "avito.ru", "page": 1, "ads_collected": 40},
    )
    rec = Recorder(mgr)
    mgr.restore()
    run_to_end(rec, task_id)
    first_request = mgr.get_task(task_id).request_log[0][2]
    assert first_request - rec.started_at[task_id] >= delay * 0.9


def test_restore_without_snapshot_uses_ads_estimate(mgr, store):
    """Нет progress -> грубая оценка pages_done = ads // 50 (не с нуля)."""
    task_id = seed_task(store, "running", fast(sites=["auto.ru"], pages=5))
    ads = [AutoRuParser(Config()).fetch_page(SPEC, {}, 1, threading.Event())][0][:1]
    batch = []
    for i in range(100):
        d = ads[0].to_dict()
        d["url"] = f"https://auto.ru/cars/restore-{i}"
        batch.append(d)
    store.save_ads(task_id, "auto.ru", batch)

    mgr.restore()
    stats = mgr.get_task(task_id).per_site["auto.ru"]
    assert stats["ads"] == 100
    assert stats["pages_done"] == 100 // MAX_ADS_PER_PAGE_ESTIMATE == 2


# ----------------------------------------------------------------------
# 9. stop для queued
# ----------------------------------------------------------------------
def test_stop_queued_task_while_another_runs(mgr, store):
    """stop для ожидающего: True, cancelled, task_stopped(id, 0); первое живёт."""
    rec = Recorder(mgr)
    first = mgr.submit(SPEC, fast(pages=2))
    second = mgr.submit(SPEC, fast(pages=2))
    pump_until(lambda: rec.of("started", first))
    assert mgr.get_status(second)["state"] == "queued"

    assert mgr.stop(second) is True
    assert mgr.get_status(second)["state"] == "cancelled"
    assert rec.of("stopped", second) == [("stopped", second, 0)]
    assert store.load_task(second)["state"] == "cancelled"

    run_to_end(rec, first)
    assert rec.of("completed", first)
    assert not rec.of("started", second)


# ----------------------------------------------------------------------
# 10. Запись в StateStore — только из потока менеджера
# ----------------------------------------------------------------------
def test_state_store_written_only_from_manager_thread(mgr, store):
    """Все записи в БД идут из потока менеджера, а не из рабочего потока."""
    manager_thread = threading.get_ident()
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=2, simulate_block="avito.ru"))
    run_to_end(rec, task_id)

    worker_threads = {
        t.name for t in threading.enumerate() if t.name.startswith("aprice-task")
    }
    assert not worker_threads  # поток завершён
    assert {"save_ads", "save_progress", "update_task_state"} <= set(store.write_threads)
    for name, idents in store.write_threads.items():
        assert idents == {manager_thread}, f"{name} вызван не из потока менеджера"


# ----------------------------------------------------------------------
# Парсеры: on_page и fetch_page (3.4b, п. 1)
# ----------------------------------------------------------------------
def test_collect_on_page_called_per_page_and_return_unchanged(config):
    """on_page вызывается после каждой страницы; collect возвращает полный список."""
    pages: list[list] = []
    parser = AutoRuParser(config)
    result = parser.collect(
        SPEC,
        {"test_fast_mode": True, "max_pages_per_site": 3},
        threading.Event(),
        on_page=lambda ads: pages.append(list(ads)),
    )
    assert len(pages) == 3
    assert [a for page in pages for a in page] == result


def test_fetch_page_single_page_no_pause_and_simulation(config):
    """fetch_page отдаёт одну страницу без пауз и поддерживает эмуляцию ошибок."""
    parser = DromParser(config)
    started = time.monotonic()
    ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert time.monotonic() - started < 0.5
    assert 20 <= len(ads) <= 50

    stop = threading.Event()
    stop.set()
    assert parser.fetch_page(SPEC, {}, 1, stop) == []
    with pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {"simulate_captcha": "drom.ru"}, 1, threading.Event())
    with pytest.raises(BlockedError):
        parser.fetch_page(SPEC, {"simulate_block": "drom.ru"}, 1, threading.Event())
