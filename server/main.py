"""Точка входа APrice-Server (подзадача 3.6): ``python -m server.main``.

Собирает готовые компоненты 3.1–3.5 в одно приложение: ``StateStore`` →
``TaskManager`` → ``IpcServer`` (+ single-instance guard) → связка
IPC↔TaskManager → восстановление → трей и окно прогресса → корректное
завершение по ``aboutToQuit``.

Решения (3.6):

* **Класс** :class:`ServerApp` вместо модульных глобалов: все ссылки на
  виджеты (``tray``, ``progress_window``, активный ``CaptchaDialog``) живут
  в его атрибутах, поэтому сборщик мусора их не уничтожит (MN-04) и тесты
  могут проверять состояние.
* **Broadcast статусов клиентам не реализуется.** Клиент опрашивает
  ``get_status`` (one-shot подключения не держат сокет); при необходимости
  push-уведомления — задача 4.
* **CAPTCHA:** пока диалог открыт, новые запросы встают в очередь и
  показываются по закрытии предыдущего (иначе пользователь не узнал бы о
  CAPTCHA на второй площадке — ``captcha_required`` по ней приходит один раз).
"""

from __future__ import annotations

import logging
import platform
import sys
import webbrowser
from collections import deque
from typing import Callable, Optional

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from common.config import Config
from common.logger import setup_logging
from common.window_manager import WindowManager
from server import protocol
from server.captcha_dialog import CaptchaDialog
from server.ipc import IpcServer
from server.progress_window import ProgressWindow
from server.single_instance import SingleInstanceGuard, show_centered_message
from server.state import StateStore
from server.task_manager import TaskManager
from server.tray import TrayIcon

logger = logging.getLogger("aprice.server.main")

SERVER_VERSION = "0.1.0"
APPLICATION_NAME = "APrice-Server"
FATAL_TITLE = "АвтоПрайс — ошибка"


# ----------------------------------------------------------------------
# Приложение
# ----------------------------------------------------------------------
def create_application(argv: list[str]) -> QApplication:
    """Создаёт (или берёт существующее) ``QApplication`` с настройками сервера.

    ``setQuitOnLastWindowClosed(False)`` — **первым делом** (MN-01): без него
    закрытие окна прогресса завершало бы приложение, а не оставляло его в
    трее.

    Args:
        argv: аргументы командной строки.

    Returns:
        QApplication: настроенное приложение.
    """
    app = QApplication.instance() or QApplication(argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APPLICATION_NAME)
    app.setApplicationVersion(SERVER_VERSION)
    return app


class ServerApp(QObject):
    """Контейнер компонентов сервера: создаёт, связывает и останавливает их.

    Конструктор только создаёт объекты; сетевой порт захватывается и GUI
    показывается в :meth:`start`. Все зависимости можно подменить (тесты).
    """

    def __init__(
        self,
        app: QApplication,
        config: Config,
        *,
        state_store: Optional[StateStore] = None,
        ipc_server: Optional[IpcServer] = None,
        window_manager: Optional[WindowManager] = None,
        open_url: Optional[Callable[[str], object]] = None,
        on_already_running: Optional[Callable[[], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """Создаёт компоненты в порядке MN-02 (до GUI).

        Args:
            app: приложение Qt.
            config: загруженная конфигурация.
            state_store: хранилище состояния (по умолчанию —
                ``data/aprice_server_state.db``).
            ipc_server: IPC-сервер (по умолчанию — ``127.0.0.1:8765``).
            window_manager: менеджер позиций окон для окна прогресса
                (по умолчанию — INI сервера).
            open_url: открытие страницы CAPTCHA в браузере (по умолчанию
                ``webbrowser.open``).
            on_already_running: действие при втором экземпляре (по умолчанию —
                окно «Сервер АвтоПрайс уже запущен»).
            parent: родительский QObject.
        """
        super().__init__(parent)
        self._app = app
        self._config = config
        self._open_url = open_url or webbrowser.open
        self._window_manager = window_manager
        self._quitted = False

        self.state_store = state_store if state_store is not None else StateStore()
        self.task_manager = TaskManager(config, self.state_store)
        self.ipc_server = ipc_server if ipc_server is not None else IpcServer()
        self.guard = SingleInstanceGuard(self.ipc_server, on_already_running)

        self.tray: Optional[TrayIcon] = None
        self.progress_window: Optional[ProgressWindow] = None
        self._active_captcha: Optional[CaptchaDialog] = None
        self._pending_captchas: deque[tuple[str, str]] = deque()

    # ------------------------------------------------------------------
    # Запуск и остановка
    # ------------------------------------------------------------------
    @property
    def active_captcha(self) -> Optional[CaptchaDialog]:
        """Текущий (удерживаемый от GC) диалог CAPTCHA или ``None``."""
        return self._active_captcha

    def start(self) -> bool:
        """Захватывает порт, связывает компоненты, восстанавливает задания, показывает GUI.

        Порядок строгий (MN-02, MN-03, MN-05): guard → связка IPC↔TaskManager →
        ``restore()`` → трей и окно → ``aboutToQuit``.

        Returns:
            bool: ``False`` — порт занят (второй экземпляр): трей не создан,
            окно о втором экземпляре уже показано; ``True`` — сервер работает.
        """
        if not self.guard.acquire():
            return False

        _connect_ipc_to_task_manager(self.ipc_server, self.task_manager, logger)
        self.task_manager.captcha_required.connect(self._on_captcha)

        restored = self.task_manager.restore()
        logger.info("Восстановлено задач: %d", len(restored))

        self._build_gui(active=bool(restored))
        self._app.aboutToQuit.connect(self.shutdown)
        return True

    def _build_gui(self, *, active: bool) -> None:
        """Создаёт трей и окно прогресса; ссылки хранятся в атрибутах (MN-04)."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("Системный трей недоступен: иконка может не отображаться")
        self.tray = TrayIcon(self.task_manager)
        self.progress_window = ProgressWindow(
            self.task_manager, window_manager=self._window_manager
        )
        self.tray.set_task_active(active)
        self.tray.show_progress_requested.connect(self._show_progress)
        self.tray.stop_requested.connect(self._stop_current)
        self.tray.quit_requested.connect(self._request_quit)
        self.progress_window.stop_requested.connect(self._stop_current)
        self.tray.show()

    def _request_quit(self) -> None:
        """Пункт «Выход»: завершает цикл событий (дальше — ``aboutToQuit``)."""
        self._app.quit()

    def shutdown(self) -> None:
        """Корректное завершение: задание, IPC, база, GUI. Идемпотентно (MN-06)."""
        if self._quitted:
            return
        self._quitted = True
        logger.info("Завершение работы сервера")
        self.task_manager.shutdown()
        self.guard.release()
        self.state_store.close()
        if self._active_captcha is not None:
            self._active_captcha.hide()
        if self.tray is not None:
            self.tray.hide()  # иначе значок «зависает» в трее Windows до наведения мыши

    # ------------------------------------------------------------------
    # Действия GUI
    # ------------------------------------------------------------------
    def _show_progress(self) -> None:
        """Двойной щелчок / «Прогресс»: показывает актуальное окно."""
        if self.progress_window is not None:
            self.progress_window.show_current()

    def _stop_current(self) -> None:
        """Останавливает выполняющееся задание (трей и кнопка окна прогресса)."""
        task_id = self.task_manager.current_task_id()
        if task_id is None:
            logger.info("Остановка запрошена, но активного задания нет")
            return
        logger.info("Остановка задания %s по запросу пользователя", task_id)
        self.task_manager.stop(task_id)

    # ------------------------------------------------------------------
    # CAPTCHA (S-13): диалог удерживается от GC и не дублируется
    # ------------------------------------------------------------------
    def _on_captcha(self, task_id: str, site: str, url: str) -> None:
        """``captcha_required`` → диалог; при открытом диалоге — в очередь."""
        if self._active_captcha is not None and self._active_captcha.isVisible():
            logger.info("CAPTCHA-диалог уже открыт; %s поставлен в очередь", site)
            self._pending_captchas.append((site, url))
            return
        self._show_captcha(site, url)

    def _show_captcha(self, site: str, url: str) -> None:
        """Создаёт и показывает диалог, сохраняя ссылку на него (КРИТИЧНО: GC)."""
        dialog = CaptchaDialog(site, url)
        dialog.open_browser_requested.connect(self._open_captcha_url)
        dialog.skip_site_requested.connect(
            lambda: logger.info("Пользователь пропустил площадку %s", site)
        )
        dialog.finished.connect(self._on_captcha_finished)
        self._active_captcha = dialog  # до show(): ссылка живёт с момента создания
        dialog.show()

    def _open_captcha_url(self, url: str) -> None:
        """«Открыть браузер»: открывает страницу CAPTCHA в браузере по умолчанию."""
        try:
            self._open_url(url)
        except Exception:  # noqa: BLE001 - сбой браузера не должен ронять сервер
            logger.exception("Не удалось открыть браузер для %s", url)

    def _on_captcha_finished(self, _result: int) -> None:
        """Диалог закрыт: показываем следующую ожидающую CAPTCHA, если есть."""
        if self._pending_captchas and not self._quitted:
            site, url = self._pending_captchas.popleft()
            self._show_captcha(site, url)


# ----------------------------------------------------------------------
# Связка IPC <-> TaskManager (п. 4)
# ----------------------------------------------------------------------
def _connect_ipc_to_task_manager(
    ipc_server: IpcServer,
    task_manager: TaskManager,
    log: Optional[logging.Logger] = None,
) -> Callable[[dict], None]:
    """Подключает обработку входящих сообщений IPC к ``TaskManager``.

    Поддерживаются ``submit_task``, ``get_status``, ``stop_task``,
    ``cancel_task``. Любое другое сообщение и любая ошибка обработки получают
    ``ErrorMsg`` (клиент ждёт ровно один ответ и иначе упёрся бы в таймаут).
    Служебное поле ``_peer`` снимается **до** передачи данных менеджеру.

    Args:
        ipc_server: IPC-сервер (сигнал ``message_received``, метод ``send_to``).
        task_manager: менеджер заданий.
        log: логгер (по умолчанию — логгер модуля).

    Returns:
        Callable: подключённый обработчик (удобно вызывать напрямую в тестах).
    """
    log = log or logger

    def reply(peer: str, msg: protocol.Message) -> None:
        if not ipc_server.send_to(peer, msg):
            log.warning("Не удалось отправить %s клиенту %s", msg.type, peer)

    def error(peer: str, request_id: str, code: str, message: str) -> None:
        reply(peer, protocol.ErrorMsg(request_id=request_id, code=code, message=message))

    def status_msg(task_id: str) -> Optional[protocol.StatusMsg]:
        status = task_manager.get_status(task_id)
        if status is None:
            return None
        return protocol.StatusMsg(
            task_id=task_id,
            state=status["state"],
            current_site=status.get("current_site"),
            progress=status.get("progress"),
            eta_seconds=status.get("eta_seconds"),
            errors=status.get("errors"),
        )

    def on_submit(peer: str, request_id: str, msg: dict) -> None:
        task_id = task_manager.submit(msg["spec"], msg.get("options") or {})
        status = task_manager.get_status(task_id) or {}
        # Позиция: выполняющееся задание + ожидающие перед новым (0 — стартует сразу).
        ahead = max(0, task_manager.queue_length() - 1)
        if task_manager.current_task_id() is not None:
            ahead += 1
        reply(
            peer,
            protocol.TaskAcceptedMsg(
                request_id=request_id,
                task_id=task_id,
                estimated_seconds=int(status.get("eta_seconds") or 0),
                queue_position=ahead,
            ),
        )

    def on_get_status(peer: str, request_id: str, msg: dict) -> None:
        task_id = msg["task_id"]
        status = status_msg(task_id)
        if status is None:
            error(peer, request_id, "unknown_task", f"Task {task_id} not found")
        else:
            reply(peer, status)

    def on_stop(peer: str, request_id: str, msg: dict) -> None:
        task_id = msg["task_id"]
        if task_manager.stop(task_id):
            reply(peer, status_msg(task_id))  # type: ignore[arg-type]
        else:
            error(
                peer, request_id, "stop_failed",
                f"Task {task_id} not found or not running",
            )

    def on_cancel(peer: str, request_id: str, msg: dict) -> None:
        task_id = msg["task_id"]
        if task_manager.cancel(task_id):
            reply(peer, status_msg(task_id))  # type: ignore[arg-type]
        else:
            error(
                peer, request_id, "cancel_failed",
                f"Task {task_id} not found or already finished",
            )

    handlers = {
        "submit_task": on_submit,
        "get_status": on_get_status,
        "stop_task": on_stop,
        "cancel_task": on_cancel,
    }

    def on_message(msg: dict) -> None:
        peer = msg.pop("_peer", None)  # служебное поле — до любой обработки
        if peer is None:
            log.error("Нет _peer в сообщении: %s", msg.get("type"))
            return
        request_id = msg.get("request_id") or ""
        msg_type = msg.get("type")
        handler = handlers.get(msg_type)
        if handler is None:
            log.warning("Неподдерживаемый тип сообщения: %s", msg_type)
            error(peer, request_id, "unsupported_type", f"Unsupported type: {msg_type}")
            return
        try:
            handler(peer, request_id, msg)
        except protocol.InvalidMessageError as exc:
            code = str(exc).split(":", 1)[0].strip() or "invalid_message"
            error(peer, request_id, code, str(exc))
        except Exception:  # noqa: BLE001 - сервер не должен падать от запроса
            log.exception("Ошибка обработки %s", msg_type)
            error(peer, request_id, "internal_error", "Internal server error")

    ipc_server.message_received.connect(on_message)
    return on_message


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    """Точка входа. Возвращает код завершения процесса.

    Коды: ``0`` — штатный выход или второй экземпляр (MN-03); ``1`` — ошибка
    инициализации, показанная окном по центру экрана (MN-07).
    """
    app = create_application(sys.argv if argv is None else argv)
    server: Optional[ServerApp] = None  # ссылка живёт до конца app.exec() (MN-04)
    try:
        config = Config().load()
        setup_logging(component="server", config=config)
        logger.info(
            "Server starting, version=%s, python=%s, platform=%s",
            SERVER_VERSION, platform.python_version(), platform.platform(),
        )
        server = ServerApp(app, config)
        if not server.start():
            logger.warning("Сервер уже запущен, выходим")
            return 0
    except Exception as exc:  # noqa: BLE001 - MN-07: любая ошибка инициализации
        logger.exception("Ошибка инициализации сервера")
        if server is not None:
            server.shutdown()
        show_centered_message(
            FATAL_TITLE, f"Не удалось запустить сервер:\n{exc}", critical=True
        )
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
