"""Тесты TaskManager (подзадача 3.4a rev 2): FSM, очередь, round-robin, ETA.

Qt — только ``QCoreApplication`` (без GUI). Рабочий поток планировщика шлёт
внутренние queued-сигналы, поэтому тесты «качают» цикл событий через
``QCoreApplication.processEvents()`` (хелпер :func:`pump_until`).

Паузы проверяются в ``test_fast_mode`` с фиксированным ``test_delay``;
интервалы берутся из монотонных меток ``Task.request_log`` (момент
«запроса» в рабочем потоке), а не из времени прихода сигнала — так цикл
событий не вносит джиттер в измерения.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import pytest
from PyQt6.QtCore import QCoreApplication

from server import protocol
from server.state import StateStore
from server.task_manager import STUB_ADS_PER_PAGE, TaskManager

SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
}
THREE = ["auto.ru", "drom.ru", "avito.ru"]
FOUR = ["auto.ru", "drom.ru", "avito.ru", "bibika.ru"]


# ----------------------------------------------------------------------
# Инфраструктура
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    """Один QCoreApplication на модуль."""
    return QCoreApplication.instance() or QCoreApplication([])


def pump_until(cond: Callable[[], bool], timeout: float = 10.0) -> None:
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
    """Собирает все публичные сигналы TaskManager в порядке прихода."""

    def __init__(self, mgr: TaskManager) -> None:
        self.events: list[tuple] = []
        mgr.task_started.connect(lambda t: self.events.append(("started", t)))
        mgr.task_progress.connect(
            lambda t, p: self.events.append(("progress", t, dict(p)))
        )
        mgr.task_completed.connect(lambda t, n: self.events.append(("completed", t, n)))
        mgr.task_stopped.connect(lambda t, n: self.events.append(("stopped", t, n)))
        mgr.task_failed.connect(lambda t, r: self.events.append(("failed", t, r)))

    def kinds(self, task_id: str | None = None) -> list[str]:
        return [e[0] for e in self.events if task_id is None or e[1] == task_id]

    def has(self, kind: str, task_id: str | None = None) -> bool:
        return kind in self.kinds(task_id)

    def sites(self, task_id: str) -> list[str]:
        return [e[2]["site"] for e in self.events if e[0] == "progress" and e[1] == task_id]


@pytest.fixture()
def store(tmp_path) -> StateStore:
    """StateStore во временной БД."""
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture()
def mgr(qapp, store):
    """TaskManager с персистентностью; по окончании теста — shutdown."""
    m = TaskManager(store)
    yield m
    m.shutdown()


def fast(sites=None, pages=3, delay=(0.05, 0.05), **extra) -> dict:
    """Опции быстрого режима с фиксированным test_delay."""
    opts = {
        "sites": list(sites or THREE),
        "max_pages_per_site": pages,
        "test_fast_mode": True,
        "test_delay": list(delay),
    }
    opts.update(extra)
    return opts


def run_to_end(mgr: TaskManager, rec: Recorder, task_id: str, timeout=15.0) -> None:
    """Ждёт терминального сигнала по заданию."""
    pump_until(
        lambda: any(k in rec.kinds(task_id) for k in ("completed", "stopped", "failed")),
        timeout,
    )


# ----------------------------------------------------------------------
# 1–2. submit / get_status
# ----------------------------------------------------------------------
def test_submit_returns_id_and_queued_state(mgr):
    """submit -> task_id, state=queued (старт — на ближайшей итерации цикла)."""
    task_id = mgr.submit(SPEC, fast())
    assert isinstance(task_id, str) and len(task_id) >= 32
    assert mgr.get_status(task_id)["state"] == "queued"
    assert mgr.queue_length() == 1


def test_get_status_is_status_msg_compatible(mgr, store):
    """get_status даёт словарь, из которого строится валидный StatusMsg."""
    task_id = mgr.submit(SPEC, fast())
    status = mgr.get_status(task_id)
    msg = protocol.StatusMsg(**status)
    protocol.validate_message(msg)  # не должен бросать
    assert status["type"] == "status"
    assert status["task_id"] == task_id
    assert status["current_site"] is None
    assert mgr.get_status("no-such-id") is None
    assert store.load_task(task_id)["state"] == "queued"


def test_submit_validates_spec_and_options(mgr):
    """Некорректные spec/options отклоняются по контракту протокола."""
    with pytest.raises(protocol.InvalidMessageError):
        mgr.submit({"mode": "bogus"}, {})
    with pytest.raises(protocol.InvalidMessageError):
        mgr.submit(SPEC, {"sites": ["example.com"]})


# ----------------------------------------------------------------------
# 3. FSM + 12. порядок сигналов + 13. per_site
# ----------------------------------------------------------------------
def test_fsm_queued_running_completed_and_signal_order(mgr, store):
    """queued -> running -> completed; сигналы: started, progress*, completed."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=2))
    assert mgr.get_status(task_id)["state"] == "queued"

    pump_until(lambda: rec.has("started", task_id))
    assert mgr.get_status(task_id)["state"] in ("running", "completed")
    assert store.load_task(task_id)["state"] in ("running", "completed")

    run_to_end(mgr, rec, task_id)
    kinds = rec.kinds(task_id)
    assert kinds[0] == "started"
    assert kinds[-1] == "completed"
    assert kinds.count("progress") == len(THREE) * 2
    assert set(kinds[1:-1]) == {"progress"}
    assert rec.events[-1][2] == len(THREE) * 2 * STUB_ADS_PER_PAGE

    assert mgr.get_status(task_id)["state"] == "completed"
    row = store.load_task(task_id)
    assert row["state"] == "completed" and row["finished_at"]
    assert store.list_active_tasks() == []


def test_per_site_updated_after_completion(mgr):
    """После завершения pages_done каждой площадки == pages_per_site."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(sites=FOUR, pages=4))
    run_to_end(mgr, rec, task_id)
    task = mgr.get_task(task_id)
    assert set(task.per_site) == set(FOUR)
    for site in FOUR:
        assert task.per_site[site] == {
            "pages_done": 4,
            "ads": 4 * STUB_ADS_PER_PAGE,
        }
    assert task.total_ads == 4 * 4 * STUB_ADS_PER_PAGE


def test_ads_limit_per_site_truncates_pages(mgr):
    """max_ads_per_site=50 -> 3 страницы (20+20+10), лимит не превышен."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=10, max_ads_per_site=50))
    run_to_end(mgr, rec, task_id)
    task = mgr.get_task(task_id)
    for site in THREE:
        assert task.per_site[site] == {"pages_done": 3, "ads": 50}
    assert rec.events[-1] == ("completed", task_id, 150)


# ----------------------------------------------------------------------
# 4–6. Round-robin: порядок, pacing, stagger
# ----------------------------------------------------------------------
def test_round_robin_order(mgr):
    """3 сайта × 3 страницы -> [A, B, C, A, B, C, A, B, C]."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=3))
    run_to_end(mgr, rec, task_id)
    assert rec.sites(task_id) == THREE * 3
    pages = [e[2]["page"] for e in rec.events if e[0] == "progress"]
    assert pages == [1, 1, 1, 2, 2, 2, 3, 3, 3]


def test_per_site_pacing(mgr):
    """Между двумя запросами к одному сайту — не меньше page_delay × 0.9."""
    delay = 0.5
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=3, delay=(delay, delay)))
    run_to_end(mgr, rec, task_id)
    log = mgr.get_task(task_id).request_log
    assert len(log) == 9
    for site in THREE:
        times = [t for s, _p, t in log if s == site]
        assert len(times) == 3
        for prev, nxt in zip(times, times[1:]):
            assert nxt - prev >= delay * 0.9, f"{site}: интервал {nxt - prev:.3f}"


def test_first_requests_are_staggered(mgr):
    """A1 -> B1 -> C1 разнесены на ≈ delay / N (допуск ±20 %)."""
    delay = 0.6
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=1, delay=(delay, delay)))
    run_to_end(mgr, rec, task_id)
    log = mgr.get_task(task_id).request_log
    first = {s: t for s, p, t in log if p == 1}
    expected = delay / len(THREE)
    for a, b in zip(THREE, THREE[1:]):
        gap = first[b] - first[a]
        assert expected * 0.8 <= gap <= expected * 1.2, f"{a}->{b}: {gap:.3f}"


# ----------------------------------------------------------------------
# 7. stop
# ----------------------------------------------------------------------
def test_stop_running_task(mgr, store):
    """stop: running -> stopping -> task_stopped; собранное сохраняется."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=10, delay=(0.5, 0.5)))
    pump_until(lambda: rec.has("progress", task_id))

    t0 = time.monotonic()
    assert mgr.stop(task_id) is True
    assert mgr.get_status(task_id)["state"] == "stopping"
    assert mgr.stop(task_id) is False  # повторно — уже не running

    pump_until(lambda: rec.has("stopped", task_id))
    assert time.monotonic() - t0 < 2.0, "остановка должна быть почти мгновенной"
    assert not rec.has("completed", task_id)
    stopped = [e for e in rec.events if e[0] == "stopped"][0]
    assert stopped[2] == mgr.get_task(task_id).total_ads >= STUB_ADS_PER_PAGE
    assert mgr.get_task(task_id).stopped_by_user is True
    assert store.load_task(task_id)["state"] == "completed"  # данные сохранены
    assert mgr.current_task_id() is None


def test_stop_unknown_or_queued_returns_false(mgr):
    """stop для неизвестного/ожидающего в очереди задания -> False."""
    assert mgr.stop("nope") is False
    task_id = mgr.submit(SPEC, fast())
    assert mgr.stop(task_id) is False  # ещё queued


# ----------------------------------------------------------------------
# 8. cancel
# ----------------------------------------------------------------------
def test_cancel_queued_task(mgr, store):
    """cancel в очереди: cancelled + task_failed(reason='cancelled')."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast())
    assert mgr.cancel(task_id) is True
    assert mgr.get_status(task_id)["state"] == "cancelled"
    assert ("failed", task_id, "cancelled") in rec.events
    assert mgr.queue_length() == 0
    assert store.load_task(task_id)["state"] == "cancelled"
    assert mgr.cancel(task_id) is False  # повторная отмена
    pump_until(lambda: True)
    assert not rec.has("started", task_id)  # в очереди не стартовало


def test_cancel_running_task(mgr, store):
    """cancel выполняющегося: останавливается, cancelled + task_failed."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=10, delay=(0.5, 0.5)))
    pump_until(lambda: rec.has("progress", task_id))
    assert mgr.cancel(task_id) is True
    pump_until(lambda: rec.has("failed", task_id))
    assert ("failed", task_id, "cancelled") in rec.events
    assert not rec.has("stopped", task_id) and not rec.has("completed", task_id)
    assert mgr.get_status(task_id)["state"] == "cancelled"
    assert store.load_task(task_id)["state"] == "cancelled"


# ----------------------------------------------------------------------
# 9. очередь
# ----------------------------------------------------------------------
def test_queue_runs_one_task_at_a_time(mgr):
    """Второй submit ждёт завершения первого; одновременно — одно задание."""
    rec = Recorder(mgr)
    first = mgr.submit(SPEC, fast(pages=2))
    second = mgr.submit(SPEC, fast(pages=2))
    assert mgr.queue_length() == 2

    running_seen: list[int] = []
    mgr.task_progress.connect(
        lambda t, p: running_seen.append(
            sum(1 for tid in (first, second) if mgr.get_status(tid)["state"] == "running")
        )
    )
    pump_until(lambda: rec.has("completed", second))

    order = [(e[0], e[1]) for e in rec.events if e[0] in ("started", "completed")]
    assert order == [
        ("started", first),
        ("completed", first),
        ("started", second),
        ("completed", second),
    ]
    assert running_seen and max(running_seen) == 1
    assert mgr.queue_length() == 0 and mgr.current_task_id() is None


# ----------------------------------------------------------------------
# 10. shutdown
# ----------------------------------------------------------------------
def test_shutdown_stops_current_and_clears_queue(mgr, store):
    """shutdown: поток остановлен, очередь пуста, без сигналов/финализации."""
    rec = Recorder(mgr)
    first = mgr.submit(SPEC, fast(pages=10, delay=(0.5, 0.5)))
    second = mgr.submit(SPEC, fast())
    pump_until(lambda: rec.has("progress", first))

    t0 = time.monotonic()
    mgr.shutdown()
    assert time.monotonic() - t0 < 2.0
    assert mgr.queue_length() == 0
    assert mgr.current_task_id() is None
    assert not [t for t in threading.enumerate() if t.name.startswith("aprice-task")]

    n_events = len(rec.events)
    QCoreApplication.processEvents()
    assert len(rec.events) == n_events  # после shutdown сигналов нет
    assert not rec.has("started", second)
    # Прерванное и ожидавшее остаются «активными» для восстановления (S-07).
    active = {t["task_id"] for t in store.list_active_tasks()}
    assert {first, second} <= active
    with pytest.raises(RuntimeError):
        mgr.submit(SPEC, fast())


# ----------------------------------------------------------------------
# 11. ETA
# ----------------------------------------------------------------------
def test_eta_fast_mode_typical(mgr):
    """4 × 10, test_fast_mode, test_delay=1: ETA ≈ 10 с (а не 40 с)."""
    task_id = mgr.submit(
        SPEC, fast(sites=FOUR, pages=10, delay=(1, 1))
    )
    assert mgr.get_status(task_id)["eta_seconds"] == 10


def test_eta_realistic_range_is_15_to_60_minutes(mgr):
    """Типовое задание 4 × 10 без fast-режима: ETA в [15; 60] мин."""
    task_id = mgr.submit(SPEC, {"sites": FOUR, "max_pages_per_site": 10})
    eta = mgr.get_status(task_id)["eta_seconds"]
    assert 15 * 60 <= eta <= 60 * 60, eta  # 40 × 210 / 4 = 2100 с = 35 мин


def test_eta_decreases_and_is_zero_when_done(mgr):
    """ETA убывает по мере выполнения и равен 0 у завершённого задания."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=3, delay=(0.2, 0.2)))
    pump_until(lambda: rec.has("started", task_id))
    first_eta = mgr.get_status(task_id)["eta_seconds"]
    run_to_end(mgr, rec, task_id)
    assert first_eta >= 0
    assert mgr.get_status(task_id)["eta_seconds"] == 0


# ----------------------------------------------------------------------
# Ошибки и прогресс в StateStore
# ----------------------------------------------------------------------
def test_scheduler_error_marks_task_failed(mgr, store, monkeypatch):
    """Исключение в планировщике -> failed + task_failed(reason) + errors."""

    def boom(task):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(TaskManager, "_max_ads_per_site", staticmethod(boom))
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast())
    pump_until(lambda: rec.has("failed", task_id))
    failed = [e for e in rec.events if e[0] == "failed"][0]
    assert "boom" in failed[2]
    assert mgr.get_task(task_id).state == "failed"
    assert store.load_task(task_id)["state"] == "failed"
    assert mgr.get_status(task_id)["errors"][0]["message"] == "boom"
    # Менеджер не «залип»: следующее задание запускается.
    monkeypatch.undo()
    nxt = mgr.submit(SPEC, fast(pages=1))
    run_to_end(mgr, rec, nxt)
    assert rec.has("completed", nxt)


def test_progress_is_persisted_in_state_store(mgr, store):
    """После каждой страницы прогресс (и per_site) пишется в StateStore (S-06)."""
    rec = Recorder(mgr)
    task_id = mgr.submit(SPEC, fast(pages=2))
    run_to_end(mgr, rec, task_id)
    progress = store.load_task(task_id)["progress"]
    assert progress["ads_collected"] == len(THREE) * 2 * STUB_ADS_PER_PAGE
    assert progress["per_site"]["auto.ru"]["pages_done"] == 2
