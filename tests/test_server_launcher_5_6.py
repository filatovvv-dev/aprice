"""5.6: автозапуск APrice-Server из Client (без реального процесса и сети)."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from client import server_launcher as sl


class Cfg:
    def __init__(self, **values):
        self.values = values

    def get(self, section, key, default=None):
        return self.values.get(key, default)


def free_port_listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_is_server_running_true_and_false():
    srv, port = free_port_listener()
    try:
        assert sl.is_server_running("127.0.0.1", port) is True
    finally:
        srv.close()
    assert sl.is_server_running("127.0.0.1", port) is False


def test_already_running_does_not_start():
    called = []
    ok = sl.ensure_server(probe=lambda: True, finder=lambda: called.append("f"), starter=lambda *a: called.append("s"))
    assert ok is True and called == []


def test_starts_server_and_waits():
    calls = []
    ok = sl.ensure_server(
        probe=lambda: False,
        finder=lambda: (["APrice-Server.exe"], Path("/x")),
        starter=lambda argv, cwd: calls.append((argv, cwd)) or True,
        waiter=lambda on_wait=None: True,
    )
    assert ok is True and calls == [(["APrice-Server.exe"], Path("/x"))]


def test_not_found_returns_false_without_start():
    assert sl.ensure_server(probe=lambda: False, finder=lambda: None,
                            starter=lambda *a: pytest.fail("нечего запускать")) is False


def test_start_failure_returns_false():
    assert sl.ensure_server(probe=lambda: False, finder=lambda: (["x"], Path(".")),
                            starter=lambda *a: False,
                            waiter=lambda **k: pytest.fail("ждать нечего")) is False


def test_wait_timeout_is_not_fatal():
    assert sl.ensure_server(probe=lambda: False, finder=lambda: (["x"], Path(".")),
                            starter=lambda *a: True, waiter=lambda on_wait=None: False) is False


def test_autostart_can_be_disabled():
    assert sl.ensure_server(Cfg(autostart_server=False), probe=lambda: False,
                            finder=lambda: pytest.fail("отключено")) is False


def test_wait_for_server_succeeds_after_retries():
    state = {"n": 0}

    def probe():
        state["n"] += 1
        return state["n"] >= 3

    waits = []
    assert sl.wait_for_server(5, probe=probe, sleep=lambda s: None, on_wait=lambda: waits.append(1)) is True
    assert len(waits) == 2


def test_wait_for_server_times_out():
    now = {"t": 0.0}
    assert sl.wait_for_server(
        1.0, probe=lambda: False,
        sleep=lambda s: now.__setitem__("t", now["t"] + s), clock=lambda: now["t"],
    ) is False


def test_find_command_prefers_exe_next_to_client(tmp_path):
    (tmp_path / "APrice-Server.exe").write_bytes(b"")
    argv, cwd = sl.find_server_command(tmp_path, frozen=True)
    assert argv == [str(tmp_path / "APrice-Server.exe")] and cwd == tmp_path


def test_find_command_legacy_sibling_folder(tmp_path):
    client = tmp_path / "Client"
    (tmp_path / "Server").mkdir()
    client.mkdir()
    (tmp_path / "Server" / "APrice-Server.exe").write_bytes(b"")
    argv, cwd = sl.find_server_command(client, frozen=True)
    assert cwd == tmp_path / "Server"


def test_find_command_dev_uses_dist_then_module(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "main.py").write_text("")
    argv, cwd = sl.find_server_command(tmp_path, frozen=False)
    assert argv[1:] == ["-m", "server.main"] and cwd == tmp_path
    (tmp_path / "dist" / "Server").mkdir(parents=True)
    (tmp_path / "dist" / "Server" / "APrice-Server.exe").write_bytes(b"")
    argv, cwd = sl.find_server_command(tmp_path, frozen=False)
    assert argv[0].endswith("APrice-Server.exe") and cwd == tmp_path / "dist" / "Server"


def test_find_command_frozen_without_exe_returns_none(tmp_path):
    assert sl.find_server_command(tmp_path, frozen=True) is None


def test_start_server_uses_popen_and_detaches(tmp_path):
    seen = {}
    assert sl.start_server(["srv"], tmp_path, popen=lambda argv, **kw: seen.update(argv=argv, kw=kw)) is True
    assert seen["argv"] == ["srv"] and seen["kw"]["cwd"] == str(tmp_path)
    assert seen["kw"]["stdout"] == subprocess.DEVNULL


def test_start_server_oserror(tmp_path):
    def boom(*a, **k):
        raise OSError("нет файла")

    assert sl.start_server(["srv"], tmp_path, popen=boom) is False


def test_client_main_calls_ensure_server_before_window(monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QApplication
    from client import main as main_module
    from common.config import Config

    order = []
    monkeypatch.setattr(main_module, "Config", lambda: Config(tmp_path / "s.json"))
    monkeypatch.setattr(main_module, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(main_module.paths, "ensure_runtime_dirs", lambda *a, **k: [])
    monkeypatch.setattr(main_module, "ensure_database", lambda cfg: True)
    monkeypatch.setattr(main_module, "ensure_server", lambda cfg, **kw: order.append("server") or True)

    class Win:
        def __init__(self, *a, **k):
            order.append("window")

        def show(self):
            pass

    class Ctl:
        def __init__(self, *a, **k):
            pass

        def start_session(self):
            order.append("session")

        def shutdown(self):
            pass

    class Wm:
        def __init__(self, *a, **k):
            pass

        def apply(self, *a):
            pass

    monkeypatch.setattr(main_module, "MainWindow", Win)
    monkeypatch.setattr(main_module, "ClientController", Ctl)
    monkeypatch.setattr(main_module, "WindowManager", Wm)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert main_module.main(["aprice-client"]) == 0
    assert order == ["server", "window", "session"]


def test_server_exits_with_session_logic_exists():
    """Остановка Server при закрытии Client — уже в ServerApp (сеанс + нет активных заданий)."""
    import inspect
    src = Path(__file__).resolve().parent.parent / "server" / "main.py"
    text = src.read_text(encoding="utf-8")
    assert "has_active_tasks()" in text and "self._app.quit()" in text
