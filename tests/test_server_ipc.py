"""Интеграционные тесты TCP IPC-сервера и синхронного клиента (подзадача 3.1).

Проверяются требования IPC-01..IPC-09, CL-01..CL-07 и список проверок
раздела 5.2 промпта. Все серверы поднимаются на ``port=0`` (фиксированный
8765 не используется — чтобы не конфликтовать с реальным Server).
Qt — ``QCoreApplication`` (без GUI). Транспортные тесты используют
``IpcClient(one_shot=True)``: сокет клиента открывается до того, как Qt
успеет обработ incoming-событие, поэтому ответ приходит в тот же recv() без
зависимости от Qt-polling (он ненадёжен в offscreen-среде CI).
"""

from __future__ import annotations

import os
import socket
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6.QtCore import QCoreApplication

from common.ipc_client import IpcClient, IpcConnectionError
from server import protocol
from server.ipc import IpcServer, find_free_port

# ---------------------------------------------------------------------------
# Фикстуры и утилиты
# ---------------------------------------------------------------------------
SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
    "transmissions": ["АКПП"],
    "fuels": ["Бензин"],
}
OPTIONS = {"sites": ["auto.ru"], "max_ads_per_site": 500, "max_pages_per_site": 10}


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    """Один QCoreApplication на модуль (QTcpServer требует event loop)."""
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def pump(seconds: float = 0.3) -> None:
    """Крутить событийный цикл Qt фиксированное время."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 2.0) -> None:
    """Крутить события Qt, пока predicate() не станет истинным.

    Raises:
        AssertionError: условие не выполнилось за timeout секунд.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("условие не выполнилось до таймаута wait_until()")


class EchoResponder:
    """Эхо-обработчик message_received: шлёт ответ обратно отправителю.

    Ответ формируется синхронно в обработчике сигнала (в момент обработки
    строки сервером), поэтому клиент может отправить запрос и затем крутить
    событийный цикл — ответ гарантированно окажется в его recv().
    """

    def __init__(self, server: IpcServer) -> None:
        self.server = server
        self.received: list[dict] = []
        server.message_received.connect(self._on_message)

    def _on_message(self, payload: dict) -> None:
        peer = payload.pop("_peer", "")
        self.received.append(payload)
        msg_type = payload.get("type")
        if msg_type == "get_status":
            reply: protocol.Message = protocol.StatusMsg(
                task_id=payload["task_id"],
                state="running",
                current_site="avito.ru",
                progress={"page": 3, "total_pages": 10, "ads_collected": 45},
                eta_seconds=1200,
                errors=[],
            )
        elif msg_type == "submit_task":
            reply = protocol.TaskAcceptedMsg(
                request_id=payload["request_id"],
                task_id="task-echo",
                estimated_seconds=3600,
                queue_position=0,
            )
        else:
            reply = protocol.ErrorMsg(
                request_id=payload.get("request_id", ""),
                code="not_handled",
                message=f"тип {msg_type!r} эхо-обработчиком не поддерживается",
            )
        self.server.send_to(peer, reply)

    def roundtrip(self, client: IpcClient, line: str) -> dict:
        """Отправить строку через сокет клиента и дождаться ответа (pump+recv)."""
        sock = client._ensure_socket()  # noqa: SLF001
        sock.sendall(line.encode("utf-8"))
        pump(1.5)
        return client._read_line(sock)  # noqa: SLF001


@pytest.fixture()
def server(qapp: QCoreApplication) -> IpcServer:
    """IpcServer на порту 0; гарантированная остановка после теста."""
    srv = IpcServer(port=0, timeout_sec=5.0)
    assert srv.start() is True
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# 1. port=0 возвращает фактический порт; stop() гасит сервер
# ---------------------------------------------------------------------------
def test_server_on_zero_port_reports_actual(qapp: QCoreApplication) -> None:
    srv = IpcServer(port=0)
    assert srv.start() is True
    actual = srv.port()
    assert 0 < actual < 65536
    srv.stop()
    assert srv.running is False


# ---------------------------------------------------------------------------
# 2. Клиент подключается, получает StatusMsg (round-trip через транспорт)
# ---------------------------------------------------------------------------
def test_client_roundtrip(server: IpcServer) -> None:
    responder = EchoResponder(server)
    client = IpcClient(port=server.port(), one_shot=True, timeout=3.0)
    answer = client.get_status("task-1")
    assert answer["type"] == "status"
    assert answer["task_id"] == "task-1"
    assert answer["state"] == "running"
    assert answer["current_site"] == "avito.ru"
    assert answer["progress"]["page"] == 3
    assert answer["eta_seconds"] == 1200
    assert len(responder.received) == 1
    assert responder.received[0]["type"] == "get_status"


# ---------------------------------------------------------------------------
# 3. Два сообщения подряд в одном долгоживущем соединении
# ---------------------------------------------------------------------------
def test_two_requests_in_one_connection(server: IpcServer) -> None:
    responder = EchoResponder(server)
    client = IpcClient(port=server.port(), timeout=3.0)
    first = client.get_status("t1")
    second = client.get_status("t2")
    assert first["task_id"] == "t1"
    assert second["task_id"] == "t2"
    assert client.connected  # соединение переиспользовалось
    assert [p["task_id"] for p in responder.received] == ["t1", "t2"]
    client.close()
    assert not client.connected


# ---------------------------------------------------------------------------
# 4. Невалидный JSON: ErrorMsg, соединение живое, следующее сообщение проходит
# ---------------------------------------------------------------------------
def test_invalid_json_does_not_break_connection(server: IpcServer) -> None:
    responder = EchoResponder(server)
    client = IpcClient(port=server.port(), timeout=3.0)
    err = client._request("error", {})  # type: ignore[attr-defined]
    # Прямого пути отправить мусор через публичный API нет — используем сокет:
    client.close()
    client = IpcClient(port=server.port(), timeout=3.0)
    sock = client._open_socket_no_ctx()  # noqa: SLF001
    client._sock = sock  # noqa: SLF001
    sock.sendall(b"this is not json\n")
    err = client._read_line(sock)  # noqa: SLF001
    assert err["type"] == "error"
    assert err["code"].startswith("invalid")
    # Соединение не разорвано (IPC-03): следующий валидный запрос проходит.
    line = '{"type":"get_status","request_id":"ok-1","task_id":"t-ok"}\n'
    sock.sendall(line.encode("utf-8"))
    answer = client._read_line(sock)  # noqa: SLF001
    assert answer["type"] == "status"
    assert answer["task_id"] == "t-ok"
    assert client.connected
    assert len(responder.received) == 1  # пришло только валидное сообщение
    client.close()


# ---------------------------------------------------------------------------
# 5. Таймаут: молчаливый клиент рвёт соединение через timeout_sec (IPC-05)
# ---------------------------------------------------------------------------
def test_idle_timeout_closes_connection(qapp: QCoreApplication) -> None:
    timeout = 0.5
    errors: list[str] = []
    srv = IpcServer(port=0, timeout_sec=timeout)
    srv.error.connect(errors.append)
    assert srv.start() is True
    sock = socket.create_connection(("127.0.0.1", srv.port()), timeout=2.0)
    wait_until(lambda: srv.clients_count() == 1)
    deadline = time.monotonic() + timeout * 8
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if srv.clients_count() == 0:
            break
        time.sleep(0.01)
    assert srv.clients_count() == 0, "таймер простоя не закрыл соединение"
    assert any("timeout" in e for e in errors)
    sock.close()
    srv.stop()


# ---------------------------------------------------------------------------
# 6. stop() закрывает все соединения и эмитит stopped (IPC-09)
# ---------------------------------------------------------------------------
def test_stop_closes_all_connections(server: IpcServer) -> None:
    client = IpcClient(port=server.port(), timeout=3.0)
    sock = client._open_socket_no_ctx()  # noqa: SLF001
    client._sock = sock  # noqa: SLF001
    wait_until(lambda: server.clients_count() == 1)

    stopped_flags: list[bool] = []
    server.stopped.connect(lambda: stopped_flags.append(True))
    server.stop()
    assert stopped_flags == [True]
    assert server.clients_count() == 0
    # Клиент видит разрыв: следующая попытка обмена падает корректно.
    with pytest.raises(Exception) as excinfo:
        client.get_status("t-after-stop")
    assert isinstance(excinfo.value, (IpcConnectionError, OSError))
    client.close()


# ---------------------------------------------------------------------------
# 7. start() при занятом порту возвращает False (основа single-instance, IPC-08)
# ---------------------------------------------------------------------------
def test_start_on_busy_port_returns_false(qapp: QCoreApplication) -> None:
    first = IpcServer(port=0)
    assert first.start() is True
    port = first.port()

    second = IpcServer(port=port)
    assert second.start() is False
    assert second.running is False

    first.stop()
    third = IpcServer(port=port)
    assert third.start() is True  # после освобождения порт снова доступен
    third.stop()


# ---------------------------------------------------------------------------
# 8. Несколько одновременных соединений (IPC-04)
# ---------------------------------------------------------------------------
def test_multiple_concurrent_clients(server: IpcServer) -> None:
    responder = EchoResponder(server)
    clients = [IpcClient(port=server.port(), timeout=3.0) for _ in range(3)]
    answers = [c.get_status(f"multi-{i}") for i, c in enumerate(clients, start=1)]
    assert server.clients_count() == 3
    assert [a["task_id"] for a in answers] == ["multi-1", "multi-2", "multi-3"]
    assert sorted(p["task_id"] for p in responder.received) == [
        "multi-1", "multi-2", "multi-3",
    ]
    for c in clients:
        c.close()


# ---------------------------------------------------------------------------
# 9. find_free_port выдаёт неиспользуемый порт
# ---------------------------------------------------------------------------
def test_find_free_port_usable(qapp: QCoreApplication) -> None:
    port = find_free_port()
    assert 0 < port < 65536
    srv = IpcServer(port=port)
    assert srv.start() is True
    assert srv.port() == port
    srv.stop()


# ---------------------------------------------------------------------------
# 10. submit_task через реальный транспорт (контракт + UTF-8)
# ---------------------------------------------------------------------------
def test_submit_task_over_transport(server: IpcServer) -> None:
    responder = EchoResponder(server)
    client = IpcClient(port=server.port(), one_shot=True, timeout=3.0)
    spec_ru = dict(SPEC, manufacturer="Лада", model="Веста")
    answer = client.submit_task(spec_ru, OPTIONS)
    assert answer["type"] == "task_accepted"
    assert answer["estimated_seconds"] == 3600
    got = responder.received[0]
    assert got["type"] == "submit_task"
    assert got["spec"]["manufacturer"] == "Лада"  # UTF-8 сквозь TCP
    client.close()


# ---------------------------------------------------------------------------
# 11. Ошибочные типы ответов: unknown_type / missing_field → ErrorMsg (IPC-03)
# ---------------------------------------------------------------------------
def test_protocol_errors_return_errormsg(server: IpcServer) -> None:
    responder = EchoResponder(server)
    client = IpcClient(port=server.port(), timeout=3.0)
    sock = client._open_socket_no_ctx()  # noqa: SLF001
    client._sock = sock  # noqa: SLF001

    cases = [
        ('{"type":"telepathy","request_id":"x"}', "unknown_type"),
        ('{"type":"get_status","request_id":"x"}', "missing_field"),
        ('{"type":"submit_task","request_id":"x","spec":{"mode":"wrong",'
         '"manufacturer":"BMW","model":"X","year_from":2020,"year_to":2020},'
         '"options":{}}', "invalid_mode"),
    ]
    for line, expected_code in cases:
        sock.sendall((line + "\n").encode("utf-8"))
        answer = client._read_line(sock)  # noqa: SLF001
        assert answer["type"] == "error", line
        assert answer["code"] == expected_code, answer
    # Соединение по-прежнему живо: валидный запрос проходит.
    sock.sendall(b'{"type":"get_status","request_id":"z","task_id":"alive"}\n')
    answer = client._read_line(sock)  # noqa: SLF001
    assert answer["task_id"] == "alive"
    client.close()
