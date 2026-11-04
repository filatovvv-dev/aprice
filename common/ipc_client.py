"""Синхронный IPC-клиент «АвтоПрайс» (для Client, тестов и CLI).

Транспорт — обычный ``socket`` (без Qt/asyncio, CL-01): соединение к
TCP-серверу Server (``127.0.0.1:8765``), отправка JSON-строки с завершающим
``\\n``, чтение одной строки ответа. Протокол сообщений — контракт
:mod:`server.protocol` (import только для констант и ErrorMsg; клиент не
разбирает сообщения в dataclass'ы, а возвращает ``dict`` — CL-05).

Два режима (CL-07):

* ``one_shot=False`` (по умолчанию) — долгоживущее соединение, все запросы
  идут в один сокет (типично для GUI Client);
* ``one_shot=True`` — открыть/закрыть на каждый вызов (удобно для тестов и CLI).

Исключения: :class:`IpcError`, :class:`IpcTimeoutError`,
:class:`IpcConnectionError`.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Optional

from common import constants
from server.protocol import MESSAGE_TYPES  # контракт: список допустимых типов


class IpcError(Exception):
    """Базовое ошибка IPC-клиента."""


class IpcTimeoutError(IpcError):
    """Превышен таймаут ожидания ответа сервера (CL-04)."""


class IpcConnectionError(IpcError):
    """Не удалось подключиться / соединение разорвано."""


class IpcClient:
    """Синхронный клиент IPC-протокола Server’а."""

    def __init__(
        self,
        host: str = constants.IPC_HOST,
        port: int = constants.IPC_PORT,
        timeout: float = constants.IPC_TIMEOUT_SEC,
        one_shot: bool = False,
    ) -> None:
        """Создать клиент.

        Args:
            host: адрес Server (по умолчанию 127.0.0.1).
            port: порт Server (по умолчанию 8765; в тестах — свободный).
            timeout: таймаут операций сокета, сек (CL-04).
            one_shot: ``True`` — открывать/закрывать соединение на каждый
                вызов; ``False`` — переиспользовать одно соединение (CL-07).
        """
        self._host = host
        self._port = port
        self._timeout = timeout
        self._one_shot = one_shot
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    # ------------------------------------------------------------- публичный API
    def submit_task(self, spec: dict, options: dict) -> dict:
        """Отправить задание (``submit_task``); вернуть ответ сервера dict'ом.

        Args:
            spec: спецификация автомобиля (валидируется на сервере).
            options: параметры сбора (sites, max_ads_per_site, ...).

        Returns:
            dict: ``task_accepted`` либо ``error`` (CL-05).
        """
        return self._request(
            "submit_task", {"spec": spec, "options": options}
        )

    def get_status(self, task_id: str) -> dict:
        """Запросить статус задания (``get_status``)."""
        return self._request("get_status", {"task_id": task_id})

    def stop_task(self, task_id: str) -> dict:
        """Остановить выполнение задания (``stop_task``)."""
        return self._request("stop_task", {"task_id": task_id})

    def cancel_task(self, task_id: str) -> dict:
        """Отменить задание — удалить из очереди и остановить (``cancel_task``)."""
        return self._request("cancel_task", {"task_id": task_id})

    def close(self) -> None:
        """Закрыть соединение, если оно открыто (CL-06)."""
        self._drop_socket()

    def __enter__(self) -> "IpcClient":
        """Контекстный менеджер: with IpcClient(...) as c: ..."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        """True, если есть активное соединение (режим one_shot=False)."""
        return self._sock is not None

    # --------------------------------------------------------------- внутреннее
    def _request(self, msg_type: str, fields: dict) -> dict:
        """Собрать сообщение, отправить, прочитать одну строку ответа.

        Raises:
            IpcTimeoutError: нет ответа дольше ``timeout``.
            IpcConnectionError: сокет закрыт сервером до полного ответа.
        """
        if msg_type not in MESSAGE_TYPES:
            raise ValueError(f"неизвестный тип запроса: {msg_type!r}")
        payload = {"type": msg_type, "request_id": str(uuid.uuid4()), **fields}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        if self._one_shot:
            with self._open_socket() as sock:
                return self._exchange(sock, line)
        if self._sock is None:
            self._sock = self._open_socket_no_ctx()
        try:
            return self._exchange(self._sock, line)
        except (IpcTimeoutError, IpcConnectionError):
            # Соединение в плохом состоянии — закрываем; следующий вызов
            # переподключится (устойчивость долгоживущего режима).
            self._drop_socket()
            raise

    def _open_socket(self) -> socket.socket:
        """Открыть сокет (контекстно-менеджируемый вариант)."""
        return self._open_socket_no_ctx()

    def _open_socket_no_ctx(self) -> socket.socket:
        """Открыть и подключить TCP-сокет к серверу.

        Raises:
            IpcConnectionError: хост/порт недоступны.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect((self._host, self._port))
        except OSError as exc:
            sock.close()
            raise IpcConnectionError(
                f"не удалось подключиться к {self._host}:{self._port}: {exc}"
            ) from exc
        return sock

    def _exchange(self, sock: socket.socket, line: str) -> dict:
        """Отправить строку и получить один ответ (строку → dict).

        Читает до первого ``\\n``; при закрытии сокета сервером без перевода
        строки бросает IpcConnectionError; при превышении таймаута —
        IpcTimeoutError (CL-04).
        """
        try:
            sock.sendall(line.encode("utf-8"))
        except OSError as exc:
            raise IpcConnectionError(f"ошибка отправки: {exc}") from exc
        return self._read_line(sock)

    def _read_line(self, sock: socket.socket) -> dict:
        """Читать байты до \\n и десериализовать JSON-ответ.

        Буфер — локальный (не атрибут клиента): тесты могут читать напрямую
        из переданного сокета; публичные методы идут через ``_exchange``
        с тем же сокетом и последовательным обменом «запрос → ответ».
        """
        buf = b""
        while True:
            index = buf.find(b"\n")
            if index != -1:
                return self._parse(buf[:index])
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise IpcTimeoutError(
                    f"нет ответа сервера дольше {self._timeout} с"
                ) from exc
            except OSError as exc:
                raise IpcConnectionError(f"ошибка чтения: {exc}") from exc
            if not chunk:
                raise IpcConnectionError("соединение закрыто сервером")
            buf += chunk

    @staticmethod
    def _parse(raw: bytes) -> dict:
        """Разобрать JSON-ответ в dict (CL-02, CL-05).

        Raises:
            IpcError: ответ не является корректным JSON-объектом.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IpcError(f"сервер вернул некорректный JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise IpcError(f"ожидался JSON-объект, получено {type(data).__name__}")
        return data

    def _drop_socket(self) -> None:
        """Закрыть долгоживущий сокет и сбросить буфер."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._buffer = b""
