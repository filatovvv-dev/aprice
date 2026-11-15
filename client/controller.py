"""Контроллер APrice-Client (подзадача 4.1): связывает окно, Server и базы данных.

* справочники (марки, модели, поколения) читаются из ``data/aprice.db``;
* ``spec`` собирается из формы (:mod:`client.form`) и уходит на Server через
  :class:`common.ipc_client.IpcClient` (``submit_task``);
* статус опрашивается раз в 2 с по ``QTimer`` (Server статусы не рассылает —
  решение 3.6); остановка — ``stop_task``;
* после ``completed`` объявления читаются **из SQLite напрямую** (только чтение,
  :mod:`client.repository`) — Server и Client на одной машине;
* статистика (min/max/median...) здесь **не считается** — это 4.2.

Решения 4.1:

* IPC-вызовы синхронные (``one_shot=True``, таймаут 5 с) и идут в GUI-потоке:
  при зависшем Server окно может «замереть» не дольше таймаута. Вынос в поток —
  отдельная задача.
* Единичный сбой опроса не обрывает ожидание: связь считается потерянной после
  :data:`MAX_POLL_FAILURES` подряд неудач.
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
from client.repository import (
    ReferenceData,
    RepositoryError,
    count_by_site,
    load_ads,
    load_reference,
)

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
        """
        super().__init__()
        self._window = window
        self._config = config
        self._ipc = ipc if ipc is not None else IpcClient(one_shot=True)
        self._reference_db = Path(reference_db or constants.DB_FILE)
        self._state_db = Path(state_db or constants.SERVER_STATE_DB)
        self._ads_loader = ads_loader
        self._reference = ReferenceData()
        self.task_id: Optional[str] = None
        self._poll_failures = 0

        self.timer = QTimer(self)
        self.timer.setInterval(poll_interval_ms)
        self.timer.timeout.connect(self.poll)

        window.evaluate_requested.connect(self.submit)
        window.stop_requested.connect(self.stop)
        window.manufacturer_changed.connect(self.on_manufacturer_changed)
        window.model_changed.connect(self.on_model_changed)
        window.generation_changed.connect(self.on_generation_changed)
        self.load_reference()

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
        """Марка выбрана: заполнить модели, сбросить зависимые поля."""
        self._window.set_models(self._reference.models(manufacturer) if manufacturer else [])
        self.on_model_changed("")

    def on_model_changed(self, model: str) -> None:
        """Модель выбрана: заполнить поколения, КПП, топливо и годы."""
        maker = self._window.form_state().manufacturer
        generations = self._reference.generations(maker, model) if maker and model else []
        self._window.set_generations([g.name for g in generations])
        self.on_generation_changed("")

    def on_generation_changed(self, generation: str) -> None:
        """Поколение выбрано (или сброшено): обновить КПП, топливо и годы."""
        state = self._window.form_state()
        maker, model = state.manufacturer, state.model
        ready = bool(maker and model)
        trans = self._reference.transmissions(maker, model, generation) if ready else []
        fuels = self._reference.fuels(maker, model, generation) if ready else []
        self._window.set_transmissions(trans or list(form.TRANSMISSIONS))
        self._window.set_fuels(fuels or list(form.FUELS))
        self._window.set_years(self._years_for(maker, model, generation))

    def _years_for(self, maker: str, model: str, generation: str) -> list[int]:
        """Годы для режима А, новые первыми.

        Пока поколение не выбрано или оно ещё выпускается, верхняя граница —
        текущий модельный год (справочник может быть неполным).
        """
        top = form.max_year()
        bounds = self._reference.year_bounds(maker, model, generation) if maker and model else None
        if bounds is None:
            low, high = 1990, top
        else:
            low, end = bounds
            high = top if (not generation or end is None) else end
        return list(range(high, low - 1, -1))

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
        """Останавливает опрос (закрытие окна)."""
        self.timer.stop()

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

    def _show_results(self, task_id: Optional[str]) -> None:
        self._window.set_status("Обработка…")
        try:
            ads = self._ads_loader(self._state_db, task_id or "")
        except RepositoryError as exc:
            logger.error("Не удалось прочитать результаты: %s", exc)
            self._window.set_status("Сбор завершён, но результаты не прочитаны")
            self._window.show_error(f"Не удалось прочитать результаты:\n{exc}")
            return
        self._window.show_results(len(ads), count_by_site(ads))
        self._window.set_status(f"Готово: собрано объявлений — {len(ads)}")

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
