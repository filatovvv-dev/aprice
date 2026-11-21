"""5.7: пауза площадки до решения CAPTCHA (чистая логика, без Qt)."""

from __future__ import annotations

import threading
import time

from server import captcha_gate as cg
from server.captcha_gate import CaptchaGate


def run_wait(gate, task="t1", site="avito.ru", stop=None):
    stop = stop or threading.Event()
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("r", gate.wait(task, site, stop)), daemon=True)
    thread.start()
    return thread, box, stop


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_resume_unblocks_waiter_with_resume():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "https://avito.ru/captcha")
    thread, box, _ = run_wait(gate)
    time.sleep(0.15)
    assert thread.is_alive()  # реально ждёт, а не пропускает площадку
    assert gate.waiting("t1") == {"avito.ru": "https://avito.ru/captcha"}
    assert gate.resume("avito.ru") is True
    thread.join(2)
    assert box["r"] == cg.RESUME and gate.waiting() == {}


def test_skip_unblocks_waiter_with_skip():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "u")
    thread, box, _ = run_wait(gate)
    assert gate.skip("avito.ru", "t1") is True
    thread.join(2)
    assert box["r"] == cg.SKIP


def test_stop_event_interrupts_wait():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "u")
    thread, box, stop = run_wait(gate)
    stop.set()
    thread.join(2)
    assert not thread.is_alive() and box["r"] is None


def test_decision_before_wait_is_not_lost():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "u")
    assert gate.resume("avito.ru") is True  # нажали до того, как поток дошёл до wait()
    assert gate.wait("t1", "avito.ru", threading.Event()) == cg.RESUME


def test_unknown_site_or_task_returns_false():
    gate = CaptchaGate()
    assert gate.resume("avito.ru") is False and gate.skip("avito.ru") is False
    gate.open("t1", "avito.ru", "u")
    assert gate.resume("drom.ru") is False and gate.resume("avito.ru", "other") is False
    assert gate.waiting() == {"avito.ru": "u"}


def test_second_decision_is_ignored():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "u")
    assert gate.resume("avito.ru") is True
    assert gate.skip("avito.ru") is False  # уже решено
    assert gate.wait("t1", "avito.ru", threading.Event()) == cg.RESUME


def test_sites_are_independent():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "a")
    gate.open("t1", "drom.ru", "b")
    assert gate.resume("drom.ru") is True
    assert gate.waiting("t1") == {"avito.ru": "a"}


def test_release_task_skips_all_waiters():
    gate = CaptchaGate()
    gate.open("t1", "avito.ru", "a")
    thread, box, _ = run_wait(gate)
    gate.release_task("t1")
    thread.join(2)
    assert box["r"] == cg.SKIP


def test_wait_without_open_returns_skip():
    assert CaptchaGate().wait("t1", "avito.ru", threading.Event()) == cg.SKIP


def test_resume_delay_is_five_seconds():
    assert cg.RESUME_DELAY_SEC == 5.0
