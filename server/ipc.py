"""TCP IPC-сервер «АвтоПрайс» (Server <-> Client).

Реализация на ``PyQt6.QtNetwork.QTcpServer`` / ``QTcpSocket`` (IPC-01):
обмен JSON-строками, разделёнными ``\\n``; десериализация и валидация —
через контракт :mod:`server.protocol`. Обработка ведётся исключительно в
GUI-потоке через сигналы Qt (IPC-07) — блокирующих вызовов нет.

Сигналы:
    * ``message_received(dict)`` — разобранные поля сообщения (включая
      ``type``); подписку выполняет TaskManager (подзадача 3.4);
    * ``error(str)`` — сетевые/протокольные проблемы (таймауты, разрывы);
    * ``stopped()`` — сервер остановлен (:meth:`IpcServer.stop`).

Сервер умеет отвечать клиенту (:meth:`IpcServer.send_to` по ``peer``-адресу),
что используется в тестах 3.1 и понадобится TaskManager в 3.4 для отправки
``task_accepted`` / ``status`` / ``error``.
"""

from __future__ import annotations

import logging
import socket

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QTcpServer, QTcpSocket

from common import constants
from server import protocol

logger = logging.getLogger("aprice.server.ipc")

#: Максимальный размер одного входящего сообщения (защита от «мусорного» трафика).
MAX_MESSAGE_BYTES: int = 1024 * 1024  # 1 MiB


def find_free_port() -> int:
    """Вернуть свободный TCP-порт на 127.0.0.1 (для автотестов).

    Порт выделяется ОС; между вызовом и ``start()`` теоретически возможна
    гонка (порт могут перехватить), но на localhost в тестах она исключена.

    Returns:
        int: номер свободного порта.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Connection:
    """Состояние одного клиентского соединения: сокет + буфер + таймер простоя."""

    def __init__(self, socket_: QTcpSocket, timeout_sec: float) -> None:
        self.sock: QTcpSocket = socket_
        self.buffer: bytearray = bytearray()
        self.timeout_ms: int = int(timeout_sec * 1000)
        # parent=socket_: таймер живёт столько же, сколько сокет соединения,
        # и не удаляется сборщиком мусора раньше времени.
        self.timer: QTimer = QTimer(socket_)
        self.timer.setSingleShot(True)

    def restart_timer(self) -> None:
        """Перезапустить таймер ожидания данных (idle-timeout)."""
        self.timer.start(self.timeout_ms)


class IpcServer(QObject):
    """TCP-сервер IPC-протокола на QTcpServer (контракт — server.protocol)."""

    #: Разобранное сообщение (dict с полем "type"); обработка — в GUI-потоке.
    message_received = pyqtSignal(dict)
    #: Текстовое описание сетевой/протокольной проблемы.
    error = pyqtSignal(str)
    #: Сервер остановлен (stop()).
    stopped = pyqtSignal()

    def __init__(
        self,
        host: str = constants.IPC_HOST,
        port: int = constants.IPC_PORT,
        timeout_sec: float = constants.IPC_TIMEOUT_SEC,
        parent: QObject | None = None,
    ) -> None:
        """Создать сервер.

        Args:
            host: адрес для прослушивания (по умолчанию 127.0.0.1).
            port: порт; ``0`` — выделить произвольный (для автотестов, IPC-08).
            timeout_sec: таймаут ожидания строки от клиента (IPC-05).
            parent: родительский QObject.
        """
        super().__init__(parent)
        self._host = host
        self._port = port
        self._timeout_sec = timeout_sec
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._connections: dict[str, _Connection] = {}
        self._running = False

    # ------------------------------------------------------------------ API
    def start(self) -> bool:
        """Начать прослушивание.

        Returns:
            bool: ``True`` при успехе; ``False``, если порт занят или
            возникла иная ошибка прослушивания (IPC-08 — основа
            single-instance guard в подзадаче 3.6).
        """
        if self._running:
            return True
        ok = self._server.listen(self._host_addr(), self._port)
        if not ok:
            logger.warning(
                "Не удалось запустить IPC-сервер на %s:%d: %s",
                self._host,
                self._port,
                self._server.errorString(),
            )
            return False
        self._running = True
        logger.info("IPC-сервер слушает %s:%d", self._host, self.port())
        return True

    def stop(self) -> None:
        """Остановить сервер: закрыть все сокеты и listener (IPC-09)."""
        self._running = False
        for conn in list(self._connections.values()):
            conn.timer.stop()
            conn.sock.disconnectFromHost()
        self._connections.clear()
        if self._server.isListening():
            self._server.close()
        logger.info("IPC-сервер остановлен")
        self.stopped.emit()

    def port(self) -> int:
        """Фактический порт прослушивания (важен при ``port=0``)."""
        return int(self._server.serverPort())

    @property
    def running(self) -> bool:
        """True, если сервер запущен."""
        return self._running

    def clients_count(self) -> int:
        """Число активных клиентских соединений (для тестов и диагностики)."""
        return len(self._connections)

    def send_to(self, peer: str, msg: protocol.Message) -> bool:
        """Отправить сообщение клиенту по адресу соединения (IPC-06).

        Args:
            peer: ключ соединения — ``"<addr>:<port>"`` из сигнала
                :attr:`message_received` (поле ``"_peer"``).
            msg: dataclass сообщения контракта.

        Returns:
            bool: ``True``, если соединение найдено и байты записаны.
        """
        conn = self._connections.get(peer)
        if conn is None:
            logger.warning("send_to: соединение %s не найдено", peer)
            return False
        try:
            payload = protocol.serialize(msg) + "\n"
        except ValueError as exc:
            logger.error("send_to: сериализация не удалась: %s", exc)
            return False
        written = conn.sock.write(payload.encode("utf-8"))
        return written > 0

    # ------------------------------------------------------------- internals
    def _host_addr(self) -> "QHostAddress":
        """QHostAddress для host (ленивый импорт — не тянуть QtNetwork в typing)."""
        from PyQt6.QtNetwork import QHostAddress

        if self._host in ("localhost", "127.0.0.1"):
            return QHostAddress(QHostAddress.SpecialAddress.LocalHost)
        return QHostAddress(self._host)

    def _on_new_connection(self) -> None:
        """Принять новое соединение, создать буфер и таймер простоя."""
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                continue
            peer = f"{sock.peerAddress().toString()}:{sock.peerPort()}"
            conn = _Connection(sock, self._timeout_sec)
            conn.timer.timeout.connect(lambda p=peer: self._on_timeout(p))
            sock.readyRead.connect(lambda c=conn: self._on_ready_read(c))
            sock.disconnected.connect(lambda c=conn, p=peer: self._on_disconnected(c, p))
            self._connections[peer] = conn
            conn.restart_timer()
            logger.debug("IPC: клиент подключился: %s", peer)

    def _drop(self, peer: str) -> None:
        """Закрыть и удалить соединение по ключу (идемпотентно)."""
        conn = self._connections.pop(peer, None)
        if conn is None:
            return
        try:
            conn.timer.stop()
        except RuntimeError:
            pass  # C++-объект таймера уже удалён вместе с сокетом
        if conn.sock.state() != QAbstractSocket.SocketState.UnconnectedState:
            conn.sock.abort()
        logger.debug("IPC: соединение закрыто: %s", peer)

    def _on_timeout(self, peer: str) -> None:
        """Нет данных дольше timeout_sec — разорвать соединение (IPC-05)."""
        logger.warning("IPC: таймаут чтения (%s) — соединение закрыто", peer)
        self._drop(peer)
        self.error.emit("timeout")

    def _on_disconnected(self, conn: _Connection, peer: str) -> None:
        """Клиент штатно отключился."""
        self._connections.pop(peer, None)
        try:
            conn.timer.stop()
        except RuntimeError:
            pass  # таймер уже удалён вместе с родительским сокетом
        logger.debug("IPC: клиент отключился: %s", peer)

    def _on_ready_read(self, conn: _Connection) -> None:
        """Накопить байты, выдать полностью принятые строки (IPC-02)."""
        peer = f"{conn.sock.peerAddress().toString()}:{conn.sock.peerPort()}"
        conn.restart_timer()  # пересчёт простоя: данные поступают
        data = conn.sock.readAll().data()
        conn.buffer.extend(data)
        if len(conn.buffer) > MAX_MESSAGE_BYTES:
            logger.error("IPC: превышен лимит сообщения от %s — соединение закрыто", peer)
            self._reply_error(conn, peer, request_id="", code="too_large",
                              message=f"сообщение превышает {MAX_MESSAGE_BYTES} байт")
            self._drop(peer)
            self.error.emit("message_too_large")
            return
        while b"\n" in conn.buffer:
            index = conn.buffer.index(b"\n")
            line_bytes = bytes(conn.buffer[:index])
            del conn.buffer[: index + 1]
            self._handle_line(line_bytes, conn, peer)
        # Если буфер растёт без \n слишком долго — сработает общий таймер простоя.

    def _handle_line(self, line_bytes: bytes, conn: _Connection, peer: str) -> None:
        """Обработать одну строку: deserialize → emit либо ErrorMsg (IPC-03)."""
        try:
            text = line_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            self._invalid(conn, peer, request_id="", detail="ошибка декодирования UTF-8")
            return
        if not text:
            return  # пустые строки-разделители игнорируются
        try:
            msg = protocol.deserialize(text)
        except protocol.InvalidMessageError as exc:
            request_id = self._extract_request_id(text)
            code = str(exc).split(":", 1)[0].strip() or "invalid_message"
            self._invalid(conn, peer, request_id=request_id, detail=str(exc), code=code)
            return
        payload = _msg_to_dict(msg)
        payload["_peer"] = peer
        logger.debug("IPC: получено %s от %s", payload.get("type"), peer)
        self.message_received.emit(payload)

    @staticmethod
    def _extract_request_id(text: str) -> str:
        """Достать request_id из некорректного JSON (best effort, для ErrorMsg)."""
        import json

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return ""
        value = data.get("request_id") if isinstance(data, dict) else None
        return value if isinstance(value, str) else ""

    def _invalid(
        self,
        conn: _Connection,
        peer: str,
        *,
        request_id: str,
        detail: str,
        code: str = "invalid_message",
    ) -> None:
        """Ответить ErrorMsg и НЕ рвать соединение (IPC-03)."""
        logger.warning("IPC: невалидное сообщение от %s: %s", peer, detail)
        self._reply_error(conn, peer, request_id=request_id, code=code, message=detail)
        self.error.emit(f"{code}: {detail}")

    def _reply_error(
        self, conn: _Connection, peer: str, *, request_id: str, code: str, message: str
    ) -> None:
        """Отправить ErrorMsg в конкретное соединение."""
        err = protocol.ErrorMsg(request_id=request_id, code=code, message=message)
        try:
            payload = protocol.serialize(err) + "\n"
            conn.sock.write(payload.encode("utf-8"))
        except Exception as exc:  # соединение могло уже умереть
            logger.debug("IPC: не удалось отправить ErrorMsg в %s: %s", peer, exc)


def _msg_to_dict(msg: object) -> dict:
    """Dataclass сообщения → dict (без импорта asdict на горячей ветке)."""
    import dataclasses

    if dataclasses.is_dataclass(msg) and not isinstance(msg, type):
        return dataclasses.asdict(msg)  # type: ignore[call-overload]
    raise TypeError(f"ожидался dataclass, получено {type(msg).__name__}")
