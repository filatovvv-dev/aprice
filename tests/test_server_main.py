"""Тесты точки входа APrice-Server (подзадача 3.6).

Работают на платформе ``offscreen`` с реальным PyQt6. Сетевые тесты идут через
настоящий ``IpcServer`` на порту ``0`` и блокирующий ``IpcClient`` в
отдельном потоке (цикл событий Qt в это время качает основной поток).
Остальное проверяется прямым вызовом обработчика сообщений с фейковым IPC.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402
import time  # noqa: E402
from typing import Callable  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, QSettings, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from common.config import Config  # noqa: E402
from common.ipc_client import IpcClient  # noqa: E402
from common.window_manager import WindowManager  # noqa: E402
from server import main as server_main  # noqa: E402
from server import protocol  # noqa: E402
from server.ipc import IpcServer  # noqa: E402
from server.main import ServerApp, _connect_ipc_to_task_manager, create_application  # noqa: E402
from server.single_instance import SingleInstanceGuard  # noqa: E402
from server.state import StateStore  # noqa: E402

SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
}
FAST = {
    "sites": ["auto.ru"],
    "max_pages_per_site": 3,
    "test_fast_mode": True, "use_stub_data": True,
    "test_delay": [0.05, 0.05],
}


# ----------------------------------------------------------------------
# Инфраструктура
# ----------------------------------------------------------------------
def pump_until(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.005)
    QApplication.processEvents()
    assert cond(), "pump_until: условие не выполнено"


def call_in_thread(qapp, fn: Callable[[], dict]) -> dict:
    """Блокирующий вызов клиента в потоке, пока основной поток качает события."""
    box: dict = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    pump_until(lambda: not thread.is_alive(), 10.0)
    thread.join(1)
    if "error" in box:
        raise box["error"]
    return box["result"]


class FakeIpc(QObject):
    """IPC-двойник: настоящий сигнал, запись ответов."""

    message_received = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, protocol.Message]] = []

    def send_to(self, peer: str, msg: protocol.Message) -> bool:
        self.sent.append((peer, msg))
        return True


@pytest.fixture()
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture()
def wm(qapp, tmp_path) -> WindowManager:
    return WindowManager(
        settings=QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat)
    )


@pytest.fixture()
def opened_urls() -> list[str]:
    return []


@pytest.fixture()
def srv(qapp, store, wm, tmp_path, opened_urls):
    """Запущенный ServerApp на свободном порту; по окончании — shutdown."""
    ipc = IpcServer(port=0, timeout_sec=5.0)
    app = ServerApp(
        qapp,
        Config(tmp_path / "settings.json"),
        state_store=store,
        ipc_server=ipc,
        window_manager=wm,
        open_url=opened_urls.append,
        on_already_running=lambda: None,
    )
    assert app.start() is True
    yield app
    app.shutdown()


@pytest.fixture()
def wired(qapp, store, tmp_path):
    """TaskManager + FakeIpc с подключённым обработчиком (без GUI)."""
    from server.task_manager import TaskManager

    mgr = TaskManager(Config(tmp_path / "settings.json"), store)
    ipc = FakeIpc()
    handler = _connect_ipc_to_task_manager(ipc, mgr)  # type: ignore[arg-type]
    yield mgr, ipc, handler
    mgr.shutdown()


def send(handler, **msg) -> None:
    handler({"_peer": "127.0.0.1:1", "request_id": "r1", **msg})


# ----------------------------------------------------------------------
# create_application / запуск
# ----------------------------------------------------------------------
def test_create_application_does_not_quit_on_last_window(qapp):
    """MN-01: закрытие окна не завершает приложение."""
    app = create_application([])
    assert app.quitOnLastWindowClosed() is False
    assert app.applicationName() == server_main.APPLICATION_NAME


def test_start_builds_components_and_listens(srv):
    """MN-02/MN-04: порт захвачен, трей и окно созданы и удерживаются."""
    assert srv.guard.acquired and srv.ipc_server.running
    assert srv.tray is not None and srv.progress_window is not None
    assert srv.tray.isVisible()


def test_second_instance_exits_without_gui(qapp, store, wm, tmp_path):
    """MN-03: порт занят → start() False, трей не создан, окно показано."""
    first = IpcServer(port=0)
    assert first.start()
    shown: list[int] = []
    second = ServerApp(
        qapp,
        Config(tmp_path / "s.json"),
        state_store=store,
        ipc_server=IpcServer(port=first.port()),
        window_manager=wm,
        on_already_running=lambda: shown.append(1),
    )
    try:
        assert second.start() is False
        assert shown == [1]
        assert second.tray is None and second.progress_window is None
    finally:
        second.task_manager.shutdown()
        first.stop()


def test_guard_acquire_release_idempotent(qapp):
    guard = SingleInstanceGuard(IpcServer(port=0), lambda: None)
    assert guard.acquire() and guard.acquire()
    guard.release()
    guard.release()
    assert not guard.acquired


# ----------------------------------------------------------------------
# IPC <-> TaskManager
# ----------------------------------------------------------------------
def test_submit_over_real_ipc_returns_task_accepted(qapp, srv):
    """Клиент → сервер: submit_task даёт task_accepted и задание в менеджере."""
    client = IpcClient(port=srv.ipc_server.port(), timeout=5, one_shot=True)
    reply = call_in_thread(qapp, lambda: client.submit_task(SPEC, FAST))
    assert reply["type"] == "task_accepted"
    assert srv.task_manager.get_status(reply["task_id"]) is not None
    assert reply["queue_position"] == 0


def test_get_status_over_real_ipc(qapp, srv):
    client = IpcClient(port=srv.ipc_server.port(), timeout=5, one_shot=True)
    accepted = call_in_thread(qapp, lambda: client.submit_task(SPEC, FAST))
    tid = accepted["task_id"]
    status = call_in_thread(qapp, lambda: client.get_status(tid))
    assert status["type"] == "status" and status["task_id"] == tid
    assert "_peer" not in status


def test_get_status_unknown_task_is_error(wired):
    mgr, ipc, handler = wired
    send(handler, type="get_status", task_id="nope")
    peer, msg = ipc.sent[-1]
    assert isinstance(msg, protocol.ErrorMsg)
    assert msg.code == "unknown_task" and msg.request_id == "r1"


def test_invalid_spec_returns_error_not_crash(wired):
    mgr, ipc, handler = wired
    send(handler, type="submit_task", spec={"mode": "range"}, options={})
    msg = ipc.sent[-1][1]
    assert isinstance(msg, protocol.ErrorMsg)
    assert mgr.queue_length() == 0


def test_unsupported_type_is_error(wired):
    mgr, ipc, handler = wired
    send(handler, type="task_started")
    msg = ipc.sent[-1][1]
    assert isinstance(msg, protocol.ErrorMsg) and msg.code == "unsupported_type"


def test_peer_is_stripped_before_manager(wired):
    """_peer снимается до обработки и в manager не попадает."""
    mgr, ipc, handler = wired
    payload = {"_peer": "p", "type": "submit_task", "request_id": "x",
               "spec": SPEC, "options": FAST}
    handler(payload)
    assert "_peer" not in payload
    assert ipc.sent[-1][0] == "p"


def test_message_without_peer_is_ignored(wired):
    mgr, ipc, handler = wired
    handler({"type": "get_status", "task_id": "x"})
    assert ipc.sent == []


def test_stop_and_cancel_flow(wired):
    mgr, ipc, handler = wired
    send(handler, type="submit_task", spec=SPEC, options=dict(FAST, test_delay=[0.3, 0.3]))
    tid = ipc.sent[-1][1].task_id
    pump_until(lambda: mgr.current_task_id() == tid)
    send(handler, type="stop_task", task_id=tid)
    assert isinstance(ipc.sent[-1][1], protocol.StatusMsg)
    send(handler, type="stop_task", task_id="nope")
    assert ipc.sent[-1][1].code == "stop_failed"
    send(handler, type="cancel_task", task_id="nope")
    assert ipc.sent[-1][1].code == "cancel_failed"
    pump_until(lambda: mgr.current_task_id() is None)


def test_queue_position_for_second_task(wired):
    mgr, ipc, handler = wired
    slow = dict(FAST, test_delay=[0.3, 0.3])
    send(handler, type="submit_task", spec=SPEC, options=slow)
    pump_until(lambda: mgr.current_task_id() is not None)
    send(handler, type="submit_task", spec=SPEC, options=slow)
    assert ipc.sent[-1][1].queue_position == 1
    for m in list(ipc.sent):
        mgr.stop(m[1].task_id)


# ----------------------------------------------------------------------
# Восстановление, GUI, завершение
# ----------------------------------------------------------------------
def test_start_restores_active_tasks(qapp, store, wm, tmp_path):
    """MN-05: queued-задание из БД попадает в очередь при запуске."""
    store.save_task("t-restore", SPEC, FAST, "queued")
    app = ServerApp(
        qapp, Config(tmp_path / "s.json"), state_store=store,
        ipc_server=IpcServer(port=0), window_manager=wm,
        on_already_running=lambda: None,
    )
    try:
        assert app.start()
        tm = app.task_manager
        # get_status умеет читать из БД, поэтому проверяем именно очередь/запуск.
        assert tm.queue_length() == 1 or tm.current_task_id() == "t-restore"
        assert app.tray is not None
    finally:
        app.shutdown()


def test_tray_actions_wired(srv, monkeypatch):
    """Пункт «Прогресс» показывает окно; «Выход» вызывает app.quit()."""
    srv.tray.show_progress_requested.emit()
    assert srv.progress_window.isVisible()
    quits: list[int] = []
    monkeypatch.setattr(srv._app, "quit", lambda: quits.append(1))
    srv.tray.quit_requested.emit()
    assert quits == [1]


def test_stop_current_without_task_is_noop(srv):
    srv._stop_current()  # не должно бросать


def test_shutdown_is_idempotent_and_releases_port(srv, monkeypatch):
    port = srv.ipc_server.port()
    calls: list[int] = []
    real = srv.task_manager.shutdown
    monkeypatch.setattr(srv.task_manager, "shutdown", lambda: (calls.append(1), real()))
    srv.shutdown()
    srv.shutdown()
    assert calls == [1]
    assert not srv.guard.acquired
    probe = IpcServer(port=port)
    try:
        assert probe.start() is True
    finally:
        probe.stop()


# ----------------------------------------------------------------------
# CAPTCHA
# ----------------------------------------------------------------------
def test_captcha_dialog_is_held_and_queued(srv):
    """S-13: диалог удерживается; второй запрос ждёт закрытия первого."""
    srv.task_manager.captcha_required.emit("t", "auto.ru", "https://auto.ru/c")
    first = srv.active_captcha
    assert first is not None and first.isVisible()
    srv.task_manager.captcha_required.emit("t", "drom.ru", "https://drom.ru/c")
    assert srv.active_captcha is first
    first.accept()
    assert srv.active_captcha is not first
    assert srv.active_captcha.isVisible()
    srv.active_captcha.accept()


def test_captcha_open_browser_uses_injected_opener(srv, opened_urls):
    srv.task_manager.captcha_required.emit("t", "auto.ru", "https://auto.ru/c")
    srv.active_captcha.open_browser_requested.emit("https://auto.ru/c")
    assert opened_urls == ["https://auto.ru/c"]
    srv.active_captcha.accept()


# ----------------------------------------------------------------------
# main()
# ----------------------------------------------------------------------
def test_main_init_error_shows_message_and_returns_1(qapp, monkeypatch):
    shown: list[tuple] = []
    monkeypatch.setattr(server_main, "show_centered_message",
                        lambda *a, **k: shown.append((a, k)))
    monkeypatch.setattr(server_main.Config, "load",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    assert server_main.main([]) == 1
    assert shown and "boom" in shown[0][0][1]


def test_main_second_instance_returns_0(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(server_main, "StateStore",
                        lambda: StateStore(tmp_path / "main.db"))
    monkeypatch.setattr(server_main, "setup_logging", lambda **k: None)
    monkeypatch.setattr(server_main.ServerApp, "start", lambda self: False)
    assert server_main.main([]) == 0


def test_all_tests_share_one_qapplication(qapp):
    """Sanity: в сессии один экземпляр, и это именно ``QApplication`` (с GUI)."""
    assert QApplication.instance() is qapp
    assert isinstance(QApplication.instance(), QApplication)
