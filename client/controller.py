"""Контроллер APrice-Client (подзадача 4.1): связывает окно, Server и базы данных.

* справочники (марки, модели, поколения) читаются из ``data/aprice.db``;
* ``spec`` собирается из формы (:mod:`client.form`) и уходит на Server через
  :class:`common.ipc_client.IpcClient` (``submit_task``);
* статус опрашивается раз в 2 с по ``QTimer`` (Server статусы не рассылает —
  решение 3.6); остановка — ``stop_task``;
* после ``completed`` объявления читаются **из SQLite напрямую** (только чтение,
  :mod:`client.repository`) — Server и Client на одной машине;
* (4.2) цены из результата считает :func:`client.stats.calculate_stats`, оценка
  сохраняется в историю (:mod:`client.history`) и показывается в окне; выбор записи
  истории восстанавливает форму и показывает сохранённые показатели **без** нового
  сбора.
* (4.3) Lifecycle: контроллер держит «сеанс» — долгоживущее соединение с Server
  (``get_active_task(session=True)``). Пока оно открыто, Server не завершается; при
  закрытии окна (:meth:`shutdown`) соединение закрывается, и Server выходит, если
  заданий нет. Если Server уже выполняет задание, Client **подхватывает** его: форма
  восстанавливается по ``spec``, опрос продолжается.

Решения 4.1:

* IPC-вызовы синхронные (``one_shot=True``, таймаут 5 с) и идут в GUI-потоке:
  при зависшем Server окно может «замереть» не дольше таймаута. Вынос в поток —
  отдельная задача.
* Единичный сбой опроса не обрывает ожидание: связь считается потерянной после
  :data:`MAX_POLL_FAILURES` подряд неудач.
* (4.4) Экспорт: контроллер хранит объявления последнего сбора и по «Экспорт»
  выгружает их в CSV/XLSX (:mod:`client.export`); «Настройки» открывает
  :class:`client.settings_dialog.SettingsDialog`; лимиты сбора из настроек
  (``client.max_pages_per_site`` / ``max_ads_per_site``) уходят Server в ``options``.
  После обновления базы из зеркала справочник перечитывается с сохранением формы.
* Ошибка записи истории не ломает показ результата — только лог. Оценка без цен
  (``count == 0``) в историю не попадает: восстанавливать из неё нечего.
* Окно используется как «утка» (методы ``set_*``/``show_*`` и сигналы), поэтому
  контроллер тестируется без настоящего GUI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QTimer

from common import constants
from common.config import Config
from common.ipc_client import IpcClient, IpcError
from client import form
from client.export import ExportError, export_ads
from client.history import describe, load_history, save_to_history
from client.repository import (
    ReferenceData,
    RepositoryError,
    count_by_site,
    load_ads,
    load_reference,
)
from client.stats import calculate_stats

logger = logging.getLogger("aprice.client.controller")

#: Интервал опроса статуса, мс (ТЗ: раз в 2 секунды).
POLL_INTERVAL_MS = 2000
#: Сколько неудачных опросов подряд считается потерей связи с Server.
MAX_POLL_FAILURES = 3
#: Состояния задания, после которых опрос прекращается.
FINAL_STATES = frozenset({"completed", "failed", "cancelled"})

SERVER_DOWN_MESSAGE = (
    "Сервер не запущен.\nЗапустите APrice-Server (python -m server.main) и повторите."
)

_STATE_TEXT = {
    "queued": "Задание в очереди…",
    "stopping": "Остановка…",
}


class ClientController(QObject):
    """Логика Client: справочники, отправка, опрос статуса, чтение результата."""

    def __init__(
        self,
        window: Any,
        config: Config,
        ipc: Optional[IpcClient] = None,
        reference_db: Optional[Path] = None,
        state_db: Optional[Path] = None,
        poll_interval_ms: int = POLL_INTERVAL_MS,
        ads_loader: Callable[[Path, str], list[dict]] = load_ads,
        history_path: Optional[Path] = None,
        session_ipc: Optional[IpcClient] = None,
        window_manager: Optional[Any] = None,
        settings_dialog_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Создаёт контроллер и подключает его к сигналам окна.

        Args:
            window: главное окно (:class:`client.gui.MainWindow` или его замена в тестах).
            config: конфигурация (``server.ipc_*`` не используются: адрес — из констант).
            ipc: IPC-клиент; по умолчанию ``IpcClient(one_shot=True)``.
            reference_db: путь к ``aprice.db`` (по умолчанию ``constants.DB_FILE``).
            state_db: путь к серверной базе (по умолчанию ``constants.SERVER_STATE_DB``).
            poll_interval_ms: период опроса статуса.
            ads_loader: чтение объявлений (подмена в тестах).
            history_path: файл истории (по умолчанию ``constants.CLIENT_HISTORY_FILE``).
            window_manager: ``WindowManager`` для позиционирования окна настроек
                (``None`` — окно не регистрируется).
            settings_dialog_factory: ``factory(config, parent, db_path=...)`` -> диалог
                настроек (подмена в тестах; по умолчанию ``SettingsDialog``).
            session_ipc: долгоживущий IPC-клиент для «сеанса» (по умолчанию
                ``IpcClient(one_shot=False)``, создаётся при первом обращении).
        """
        super().__init__()
        self._window = window
        self._config = config
        self._ipc = ipc if ipc is not None else IpcClient(one_shot=True)
        self._reference_db = Path(reference_db or constants.DB_FILE)
        self._state_db = Path(state_db or constants.SERVER_STATE_DB)
        self._ads_loader = ads_loader
        self._history_path = Path(history_path) if history_path is not None else None
        self._history: list[dict] = []
        self._spec: Optional[dict] = None
        self._session_ipc = session_ipc
        self._window_manager = window_manager
        self._dialog_factory = settings_dialog_factory
        self._last_ads: list[dict] = []
        self._reload_pending = False
        self._session_open = False
        self._reference = ReferenceData()
        self.task_id: Optional[str] = None
        self._poll_failures = 0

        self.timer = QTimer(self)
        self.timer.setInterval(poll_interval_ms)
        self.timer.timeout.connect(self.poll)

        window.evaluate_requested.connect(self.submit)
        window.stop_requested.connect(self.stop)
        window.manufacturer_changed.connect(self.on_manufacturer_changed)
        window.history_selected.connect(self.on_history_selected)
        window.export_requested.connect(self.on_export_requested)
        window.settings_requested.connect(self.open_settings)
        self.load_reference()
        self.refresh_history()

    # ------------------------------------------------------------------
    # Справочники
    # ------------------------------------------------------------------
    def load_reference(self) -> bool:
        """Загружает справочники и заполняет окно; ``False``, если база недоступна."""
        try:
            self._reference = load_reference(self._reference_db)
        except RepositoryError as exc:
            logger.error("Справочник недоступен: %s", exc)
            self._reference = ReferenceData()
            self._window.set_manufacturers([])
            self._window.set_status(f"Справочник недоступен: {exc}")
            return False
        self._window.set_manufacturers(self._reference.manufacturers())
        self.on_manufacturer_changed("")
        return True

    def on_manufacturer_changed(self, manufacturer: str) -> None:
        """Марка выбрана: заполнить список моделей (модель сбрасывается)."""
        self._window.set_models(self._reference.models(manufacturer) if manufacturer else [])

    # ------------------------------------------------------------------
    # Отправка, опрос, остановка
    # ------------------------------------------------------------------
    def submit(self) -> None:
        """Отправляет задание на Server и запускает опрос статуса."""
        state = self._window.form_state()
        error = form.form_error(state)
        if error is not None:
            self._window.show_error(error)
            return
        spec, options = form.build_spec(state), form.build_options()
        options.update(self._limit_options())
        self._spec = spec
        self._last_ads = []
        self._window.clear_results()
        self._window.set_running(True)
        self._window.set_status("Отправка задания…")
        logger.info("Отправка задания: %s", spec)
        try:
            reply = self._ipc.submit_task(spec, options)
        except IpcError as exc:
            logger.error("Server недоступен: %s", exc)
            self._reset_after_failure(SERVER_DOWN_MESSAGE)
            return
        if reply.get("type") != "task_accepted":
            self._reset_after_failure(self._error_text(reply))
            return
        self.task_id = str(reply["task_id"])
        self._poll_failures = 0
        self._open_session()  # Server мог быть запущен позже Client — сеанс ещё не открыт
        position = reply.get("queue_position")
        suffix = f" (впереди заданий: {position})" if position else ""
        self._window.set_status(f"Задание принято{suffix}")
        self.timer.start()

    def stop(self) -> None:
        """Просит Server остановить задание; опрос продолжается до финального статуса."""
        if self.task_id is None:
            return
        try:
            reply = self._ipc.stop_task(self.task_id)
        except IpcError as exc:
            logger.error("Не удалось отправить stop_task: %s", exc)
            self._window.show_error(SERVER_DOWN_MESSAGE)
            return
        if reply.get("type") == "error":
            self._window.show_error(self._error_text(reply))
            return
        self._window.set_status("Остановка…")

    def poll(self) -> None:
        """Один шаг опроса статуса (вызывается по таймеру раз в 2 с)."""
        if self.task_id is None:
            self.timer.stop()
            return
        try:
            reply = self._ipc.get_status(self.task_id)
        except IpcError as exc:
            self._poll_failures += 1
            logger.warning("Опрос статуса не удался (%d): %s", self._poll_failures, exc)
            if self._poll_failures >= MAX_POLL_FAILURES:
                self._finish_failed("Связь с сервером потеряна.\n" + SERVER_DOWN_MESSAGE)
            return
        self._poll_failures = 0
        if reply.get("type") == "error":
            self._finish_failed(self._error_text(reply))
            return
        state = reply.get("state")
        if state in FINAL_STATES:
            self._finish(state, reply)
        else:
            self._window.set_status(self._status_text(state, reply))

    def shutdown(self) -> None:
        """Закрытие окна: останавливает опрос и закрывает «сеанс» (Server может выйти)."""
        self.timer.stop()
        self._close_session()

    # ------------------------------------------------------------------
    # Жизненный цикл (4.3)
    # ------------------------------------------------------------------
    def start_session(self) -> bool:
        """Открывает «сеанс» с Server и подхватывает активное задание, если оно есть.

        Вызывается при старте окна. Server не запущен — не ошибка: сеанс откроется при
        первой успешной отправке задания (:meth:`submit`).

        Returns:
            bool: ``True``, если задание подхвачено.
        """
        reply = self._open_session()
        if reply is None or reply.get("type") != "active_task" or not reply.get("task_id"):
            return False
        return self._adopt_task(reply)

    def _open_session(self) -> Optional[dict]:
        """Один запрос ``get_active_task(session=True)``; ``None``, если Server недоступен."""
        if self._session_open:
            return None
        if self._session_ipc is None:
            self._session_ipc = IpcClient(one_shot=False)
        try:
            reply = self._session_ipc.get_active_task(session=True)
        except IpcError as exc:
            logger.info("Сеанс с Server не открыт (%s)", exc)
            return None
        self._session_open = reply.get("type") == "active_task"
        return reply

    def _close_session(self) -> None:
        if self._session_ipc is not None:
            try:
                self._session_ipc.close()
            except Exception:  # noqa: BLE001 — закрытие не должно ломать выход
                logger.debug("Сеанс закрыт с ошибкой", exc_info=True)
        self._session_open = False

    def _adopt_task(self, reply: dict) -> bool:
        """Подхватывает задание Server: форма по ``spec``, индикатор «занят», опрос."""
        if self.task_id is not None:
            return False
        self.task_id = str(reply["task_id"])
        spec = reply.get("spec") if isinstance(reply.get("spec"), dict) else {}
        self._spec = spec
        self._poll_failures = 0
        self._restore_form(spec)
        self._window.clear_results()
        self._window.set_running(True)
        self._window.set_status("Подхвачено активное задание сервера")
        logger.info("Подхвачено активное задание %s", self.task_id)
        self.timer.start()
        return True

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------
    def _finish(self, state: str, reply: dict) -> None:
        task_id = self.task_id
        self.timer.stop()
        self._window.set_running(False)
        self.task_id = None
        if state == "completed":
            self._show_results(task_id)
        elif state == "cancelled":
            self._window.set_status("Задание отменено")
        else:
            self._window.set_status("Сбор завершился ошибкой")
            self._window.show_error(self._failure_text(reply))
        if self._reload_pending:
            self._reload_reference_keep_form()

    def _show_results(self, task_id: Optional[str]) -> None:
        self._window.set_status("Обработка…")
        try:
            ads = self._ads_loader(self._state_db, task_id or "")
        except RepositoryError as exc:
            logger.error("Не удалось прочитать результаты: %s", exc)
            self._window.set_status("Сбор завершён, но результаты не прочитаны")
            self._window.show_error(f"Не удалось прочитать результаты:\n{exc}")
            return
        self._last_ads = ads
        self._window.set_export_enabled(bool(ads))
        prices = [ad.get("price") for ad in ads]
        stats = calculate_stats(prices)
        self._window.update_charts(prices)
        self._window.show_results(len(ads), count_by_site(ads), stats["count"])
        self._window.update_stats(stats)
        self._window.set_status(f"Готово: собрано объявлений — {len(ads)}")
        if stats["count"] > 0 and self._spec is not None:
            try:
                save_to_history(stats, self._spec, path=self._history_path)
            except OSError as exc:
                logger.error("История запросов не сохранена: %s", exc)
            self.refresh_history()

    # ------------------------------------------------------------------
    # Экспорт, настройки (4.4)
    # ------------------------------------------------------------------
    def export_data(self, file_path: Any, site_data: Optional[list] = None) -> int:
        """Записывает объявления в ``.csv``/``.xlsx``; по умолчанию — последнего сбора.

        Raises:
            ExportError: формат не поддерживается или файл не записан.
        """
        ads = self._last_ads if site_data is None else site_data
        return export_ads(file_path, ads)

    def on_export_requested(self) -> None:
        """Кнопка «Экспорт»: выбор файла и выгрузка; ошибки — диалогом, без падения."""
        if not self._last_ads:
            self._window.show_error("Нет данных для экспорта: сначала выполните оценку.")
            return
        path = self._window.choose_export_path()
        if not path:
            return
        try:
            count = self.export_data(path)
        except ExportError as exc:
            logger.error("Экспорт не выполнен: %s", exc)
            self._window.show_error(str(exc))
            return
        self._window.set_status(f"Экспортировано объявлений: {count} → {path}")

    def _limit_options(self) -> dict:
        """Лимиты сбора из настроек (``client.*``); некорректные значения пропускаются."""
        limits: dict = {}
        for key in ("max_pages_per_site", "max_ads_per_site"):
            value = self._config.get("client", key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
        return limits

    def open_settings(self) -> None:
        """Кнопка «Настройки»: модальное окно, позиция — через ``WindowManager``."""
        factory = self._dialog_factory or self._default_dialog_factory
        dialog = factory(self._config, self._window, db_path=self._reference_db)
        dialog.database_updated.connect(self._on_database_updated)
        if self._window_manager is not None:
            self._window_manager.apply(dialog, "settings_dialog")
        dialog.exec()

    @staticmethod
    def _default_dialog_factory(config: Any, parent: Any, db_path: Optional[Path] = None) -> Any:
        from client.settings_dialog import SettingsDialog

        return SettingsDialog(config, parent, db_path=db_path)

    def _on_database_updated(self, _result: Any) -> None:
        """Справочник заменён: перечитать (во время сбора — после его окончания)."""
        if self.task_id is not None:
            self._reload_pending = True
            self._window.set_status("Справочник обновлён; форма обновится после сбора")
            return
        self._reload_reference_keep_form()
        self._window.set_status("Справочник обновлён")

    def _reload_reference_keep_form(self) -> None:
        """Перечитывает справочник и возвращает выбранные в форме значения."""
        self._reload_pending = False
        state = self._window.form_state()
        if self.load_reference():
            self._restore_form(self._spec_from_state(state))

    @staticmethod
    def _spec_from_state(state: form.FormState) -> dict:
        """Состояние формы в виде словаря, понятного :meth:`_restore_form` (без проверок)."""
        return {
            "mode": form.MODE_RANGE,
            "manufacturer": state.manufacturer,
            "model": state.model,
            "year_from": state.year_from,
            "year_to": state.year_to,
            "engine_volume_from": state.volume_from or None,
            "engine_volume_to": state.volume_to or None,
            "transmissions": list(state.transmissions),
            "fuels": list(state.fuels),
        }

    # ------------------------------------------------------------------
    # История запросов
    # ------------------------------------------------------------------
    def refresh_history(self) -> None:
        """Перечитывает историю и обновляет выпадающий список."""
        self._history = load_history(self._history_path)
        self._window.set_history([describe(entry) for entry in self._history])

    def on_history_selected(self, index: int) -> None:
        """Просмотр записи истории: форма и показатели восстанавливаются, сбор не идёт."""
        if self.task_id is not None or not 0 <= index < len(self._history):
            return
        entry = self._history[index]
        self._last_ads = []  # в истории объявления не хранятся — экспортировать нечего
        spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}
        self._restore_form(spec)
        self._window.clear_results()
        self._window.show_results(int(entry.get("count") or 0), {})
        self._window.update_stats(entry)
        self._window.set_status(f"Сохранённый результат: {describe(entry)}")

    def _restore_form(self, spec: dict) -> None:
        """Выставляет форму по ``spec`` каскадом: марка -> модель -> диапазоны.

        Старые записи истории с ``mode: "exact"`` (до 4.6) читаются как диапазон
        ``год от = год до``: у них уже есть ``year_from``/``year_to`` (либо только
        ``year_from``); поколение, если было, игнорируется, а единственные КПП и
        топливо становятся отмеченными чекбоксами.
        """
        window = self._window
        maker = str(spec.get("manufacturer") or "")
        window.select_manufacturer(maker)
        self.on_manufacturer_changed(maker)
        window.select_model(str(spec.get("model") or ""))
        year_from = spec.get("year_from")
        year_to = spec.get("year_to")
        if year_to is None:
            year_to = year_from
        if year_from is None:
            year_from = year_to
        window.set_range(
            year_from, year_to, spec.get("engine_volume_from"), spec.get("engine_volume_to")
        )
        window.set_checked(list(spec.get("transmissions") or []), list(spec.get("fuels") or []))

    def _finish_failed(self, message: str) -> None:
        self.timer.stop()
        self.task_id = None
        self._reset_after_failure(message)

    def _reset_after_failure(self, message: str) -> None:
        self._window.set_running(False)
        self._window.set_status("Ошибка")
        self._window.show_error(message)

    # ------------------------------------------------------------------
    # Тексты
    # ------------------------------------------------------------------
    @staticmethod
    def _error_text(reply: dict) -> str:
        """Текст ошибки Server (``error``) для пользователя."""
        message = reply.get("message") or reply.get("code") or "неизвестная ошибка"
        return f"Сервер вернул ошибку: {message}"

    @staticmethod
    def _failure_text(reply: dict) -> str:
        """Причина ``failed``: площадки и виды ошибок из ``errors`` статуса."""
        errors = reply.get("errors") or []
        parts = [
            f"{e.get('site', '?')}: {e.get('kind') or e.get('message') or '?'}"
            for e in errors
            if isinstance(e, dict)
        ]
        return "Не удалось собрать объявления." + (("\n" + "\n".join(parts)) if parts else "")

    @staticmethod
    def _status_text(state: Any, reply: dict) -> str:
        """Подпись статуса: площадка, страница и число объявлений из ``progress``."""
        if state in _STATE_TEXT:
            return _STATE_TEXT[state]
        progress = reply.get("progress") or {}
        site = reply.get("current_site") or progress.get("site")
        text = f"Сбор данных с {site}…" if site else "Сбор данных…"
        details = []
        if progress.get("page"):
            details.append(f"страница {progress['page']}")
        if progress.get("ads_collected") is not None:
            details.append(f"объявлений {progress['ads_collected']}")
        eta = reply.get("eta_seconds")
        if eta:
            details.append(f"осталось ≈ {int(eta) // 60 + 1} мин")
        return text + (f" ({', '.join(details)})" if details else "")
