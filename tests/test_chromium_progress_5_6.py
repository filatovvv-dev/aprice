"""5.6: прогресс установки Chromium — разбор процентов/скорости и передача в диалог."""

from __future__ import annotations

import io
import subprocess

import pytest

from server import browser_check as bc


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_parses_percent_and_speed_from_line():
    p = bc.ProgressParser()
    assert p.feed("Downloading Chromium 130.0 ... 45% 1.2 MB/s") == (45, "1.2 MB/s")


def test_parses_kib_speed():
    assert bc.ProgressParser().feed("10% 850 KiB/s") == (10, "850 KB/s")


def test_speed_computed_from_total_and_elapsed():
    clock = Clock()
    p = bc.ProgressParser(clock)
    assert p.feed("|■■        |  10% of 100.0 MiB")[0] == 10
    clock.t = 5.0
    percent, speed = p.feed("|■■■■■     |  50% of 100.0 MiB")
    assert percent == 50 and speed == "10.0 MB/s"  # 50 МБ за 5 с


def test_unrecognized_or_repeated_lines_are_ignored():
    p = bc.ProgressParser()
    assert p.feed("Downloading Chromium from https://x") is None
    assert p.feed("random 150 words") is None
    assert p.feed("45%") == (45, None)
    assert p.feed("45%") is None  # не изменилось
    assert p.feed("999%") is None  # вне диапазона


def test_progress_text_variants():
    assert bc.progress_text(None, None) == "Установка Chromium..."
    assert bc.progress_text(45, "1.2 MB/s") == "Скачивание Chromium: 45% · 1.2 MB/s"
    assert bc.progress_text(7, None) == "Скачивание Chromium: 7%"


class FakeProc:
    def __init__(self, payload: bytes, code=0):
        self.stdout = io.BufferedReader(io.BytesIO(payload))
        self.code = code
        self.killed = False

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.killed = True


def test_run_streaming_reports_progress_from_cr_separated_output():
    payload = (b"Downloading Chromium 130\r|##  | 10% of 100 MiB 2.0 MB/s\r"
               b"|#####| 50% of 100 MiB 3.0 MB/s\r|#####| 100% of 100 MiB 4.0 MB/s\nDone\n")
    got = []
    holder = {}

    def popen(argv, **kw):
        holder["kw"] = kw
        return FakeProc(payload)

    res = bc.run_streaming(["x"], on_progress=lambda p, s: got.append((p, s)), popen=popen)
    assert got == [(10, "2.0 MB/s"), (50, "3.0 MB/s"), (100, "4.0 MB/s")]
    assert res.returncode == 0 and "Done" in res.stdout
    assert holder["kw"]["stderr"] == subprocess.STDOUT and holder["kw"]["stdout"] == subprocess.PIPE


def test_run_streaming_without_percent_never_calls_back():
    got = []
    res = bc.run_streaming(["x"], on_progress=lambda *a: got.append(a),
                           popen=lambda argv, **kw: FakeProc(b"Installing...\nno numbers here\n"))
    assert got == [] and res.returncode == 0  # UI остаётся indeterminate


def test_run_streaming_nonzero_exit_and_callback_errors_dont_break():
    def bad(*a):
        raise RuntimeError("ui")

    res = bc.run_streaming(["x"], on_progress=bad, popen=lambda argv, **kw: FakeProc(b"50%\nerr\n", code=1))
    assert res.returncode == 1 and "err" in res.stdout


def test_run_streaming_timeout_kills_process():
    class Hang(FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)

    proc = Hang(b"")
    with pytest.raises(subprocess.TimeoutExpired):
        bc.run_streaming(["x"], popen=lambda argv, **kw: proc, timeout=0.01)
    assert proc.killed is True


def test_install_chromium_passes_callback_to_runner(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    seen = {}

    def runner(argv, **kw):
        seen["cb"] = kw.get("on_progress")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    cb = lambda p, s: None
    assert bc.install_chromium(runner, environ={}, on_progress=cb)[0] is True
    assert seen["cb"] is cb
    seen.clear()
    bc.install_chromium(runner, environ={})  # без callback — runner не получает лишнего kw
    assert seen["cb"] is None


def test_cdn_fallback_resets_progress_to_indeterminate(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    events = []

    def runner(argv, on_progress=None, **kw):
        host = kw["env"].get("PLAYWRIGHT_DOWNLOAD_HOST")
        on_progress(30, None) if host else on_progress(5, None)
        return subprocess.CompletedProcess(argv, 1 if host else 0, stdout="", stderr="")

    ok, _ = bc.install_chromium(runner, environ={}, on_progress=lambda p, s: events.append(p))
    assert ok is True and events == [30, None, 5]


class UiWithProgress:
    def __init__(self):
        self.reports = []

    def report_progress(self, percent, speed):
        self.reports.append((percent, speed))

    def run(self, work):
        return work()

    def show_error(self, message):
        pass


def test_ensure_chromium_hands_ui_progress_to_installer():
    ui = UiWithProgress()
    state = {"done": False}

    def installer(on_progress=None):
        on_progress(45, "1.2 MB/s")
        state["done"] = True
        return True, ""

    assert bc.ensure_chromium(ui, is_installed=lambda: state["done"], installer=installer) is True
    assert ui.reports == [(45, "1.2 MB/s")]


def test_ensure_chromium_without_progress_ui_calls_installer_plain():
    from tests.test_build_5_1 import FakeUi

    ui = FakeUi()
    state = {"done": False}

    def installer():
        state["done"] = True
        return True, ""

    assert bc.ensure_chromium(ui, is_installed=lambda: state["done"], installer=installer) is True
