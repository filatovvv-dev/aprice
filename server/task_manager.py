"""TaskManager APrice-Server: FSM задания, очередь, round-robin, ETA (3.4a rev 2).

Модуль содержит:

* :class:`Task` — состояние одного задания (в т.ч. ``per_site`` для ETA);
* :class:`TaskManager` — очередь (одно задание одновременно), FSM и
  планировщик сбора.

Планировщик — **round-robin**: запросы к разным площадкам чередуются,
первые запросы площадок разнесены на ``avg_delay / N``, а каждая
площадка видит запрос строго раз в ``page_delay`` (per-site pacing).
В 3.4a «запрос» — мгновенная заглушка (реальные парсеры — 3.4b).

Потоки
------
Планировщик (:meth:`TaskManager._process_task`) выполняется в рабочем
потоке: он спит через :func:`common.utils.interruptible_sleep` и потому
не должен блокировать GUI. ``StateStore`` однопоточный
(``check_same_thread=True``), поэтому рабочий поток **не** обращается к
БД и не шлёт публичные сигналы: он испускает внутренние сигналы
``_sig_progress`` / ``_sig_finished`` с ``QueuedConnection``, а запись в
БД и публичные сигналы выполняются в потоке, где живёт ``TaskManager``
(для этого в нём должен работать цикл событий Qt).

Состояния
---------
``queued → running → completed`` | ``running → stopping → (остановлено)``
| ``→ failed`` | ``queued/running → cancelled``. Состояния ``stopped`` в
замороженном контракте (``protocol.TASK_STATES``) нет, поэтому задание,
остановленное по запросу клиента (S-12), получает состояние
``completed`` (собранное сохранено), а факт остановки передаётся
сигналом ``task_stopped`` и флагом :attr:`Task.stopped_by_user`.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from common import constants
from common.utils import interruptible_sleep
from server import protocol
from server.parsers.base import Ad
from server.state import StateStore

logger = logging.getLogger("aprice.server.task_manager")

#: Размер страницы выдачи в заглушке 3.4a (объявлений за «запрос»).
STUB_ADS_PER_PAGE: int = 20

#: Диапазон паузы между страницами одной площадки в ``test_fast_mode``, сек.
FAST_PAGE_DELAY_SEC: tuple[float, float] = (0.1, 0.3)

#: Сколько секунд ждать завершения рабочего потока в ``shutdown``.
SHUTDOWN_JOIN_TIMEOUT_SEC: float = 5.0


def _utc_now_iso() -> str:
    """Возвращает текущее время UTC в ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# Task
# ----------------------------------------------------------------------
@dataclass
class Task:
    """Состояние одного задания на сбор.

    Служебные поля (``stop_event``, блокировка, флаги, монотонные метки)
    не входят в публичный контракт и скрыты из ``repr``.
    """

    task_id: str
    spec: dict
    options: dict
    state: str = "queued"
    created_at: str = field(default_factory=_utc_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    #: Последний прогресс: ``{"site": ..., "page": ..., "ads_collected": ...}``.
    progress: dict = field(default_factory=dict)
    #: Для ETA и статуса: ``{site: {"pages_done": int, "ads": int}}``.
    per_site: dict[str, dict] = field(default_factory=dict)
    collected: dict[str, list[Ad]] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    # ------------------------- служебные поля -------------------------
    stop_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )
    lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    #: Монотонная метка старта (для ETA и pacing; не зависит от системных часов).
    started_monotonic: Optional[float] = field(default=None, repr=False)
    #: Журнал запросов планировщика: ``(site, page, time.monotonic())``.
    request_log: list[tuple[str, int, float]] = field(default_factory=list, repr=False)
    stopped_by_user: bool = field(default=False, repr=False)
    cancel_requested: bool = field(default=False, repr=False)
    failure_reason: Optional[str] = field(default=None, repr=False)

    @property
    def total_ads(self) -> int:
        """Суммарное число собранных объявлений по всем площадкам."""
        with self.lock:
            return sum(int(s.get("ads", 0)) for s in self.per_site.values())

    def to_status_dict(self, eta_seconds: Optional[int] = None) -> dict[str, Any]:
        """Возвращает словарь, совместимый с ``protocol.StatusMsg``.

        Args:
            eta_seconds: расчётное оставшееся время (считает TaskManager).

        Returns:
            dict: ``type``, ``task_id``, ``state``, ``current_site``
            (последняя обработанная площадка), ``progress``,
            ``eta_seconds``, ``errors``.
        """
        with self.lock:
            progress = dict(self.progress)
            return {
                "type": "status",
                "task_id": self.task_id,
                "state": self.state,
                "current_site": progress.get("site"),
                "progress": progress or None,
                "eta_seconds": eta_seconds,
                "errors": [dict(e) for e in self.errors] or None,
            }


# ----------------------------------------------------------------------
# TaskManager
# ----------------------------------------------------------------------
class TaskManager(QObject):
    """Очередь заданий, FSM и round-robin планировщик сбора.

    Не более одного задания выполняется одновременно; остальные ждут в
    очереди FIFO. Все публичные методы вызываются из потока, в котором
    создан менеджер (GUI-поток Server).

    Signals:
        task_started(task_id): задание перешло в ``running``.
        task_progress(task_id, progress): после каждого «запроса».
        task_completed(task_id, total_ads): задание выполнено.
        task_stopped(task_id, total_ads): остановлено клиентом (S-12).
        task_failed(task_id, reason): ошибка или отмена (``"cancelled"``).
        captcha_required(task_id, site, url): CAPTCHA (используется в 3.4b).
    """

    task_started = pyqtSignal(str)
    task_progress = pyqtSignal(str, dict)
    task_completed = pyqtSignal(str, int)
    task_stopped = pyqtSignal(str, int)
    task_failed = pyqtSignal(str, str)
    captcha_required = pyqtSignal(str, str, str)

    # Внутренние сигналы: рабочий поток -> поток менеджера (queued).
    _sig_progress = pyqtSignal(str, dict)
    _sig_finished = pyqtSignal(str)

    def __init__(
        self, state_store: Optional[StateStore] = None, parent: Optional[QObject] = None
    ) -> None:
        """Инициализирует менеджер.

        Args:
            state_store: хранилище состояния (``None`` — без персистентности,
                удобно для тестов планировщика).
            parent: родительский QObject.
        """
        super().__init__(parent)
        self._store = state_store
        self._tasks: dict[str, Task] = {}
        self._queue: deque[str] = deque()
        self._current_id: Optional[str] = None
        self._worker: Optional[threading.Thread] = None
        self._shutting_down = False
        self._sig_progress.connect(
            self._on_progress, Qt.ConnectionType.QueuedConnection
        )
        self._sig_finished.connect(
            self._on_finished, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def submit(self, spec: dict, options: Optional[dict] = None) -> str:
        """Ставит задание в очередь.

        Старт задания (``queued → running``) выполняется на ближайшей
        итерации цикла событий, поэтому сразу после ``submit`` состояние —
        ``queued``.

        Args:
            spec: спецификация задания (валидируется по контракту).
            options: опции задания; помимо контрактных (``sites``,
                ``max_ads_per_site``, ``max_pages_per_site``) допускаются
                служебные ``test_fast_mode`` и ``test_delay``.

        Returns:
            str: идентификатор задания (UUID v4).

        Raises:
            protocol.InvalidMessageError: некорректные ``spec``/``options``.
            RuntimeError: менеджер остановлен (``shutdown``).
        """
        if self._shutting_down:
            raise RuntimeError("TaskManager остановлен")
        raw_options = dict(options or {})
        protocol.validate_spec(spec)
        normalized = protocol.validate_options(raw_options)
        merged = {**raw_options, **normalized}

        task = Task(task_id=str(uuid.uuid4()), spec=dict(spec), options=merged)
        self._tasks[task.task_id] = task
        self._queue.append(task.task_id)
        if self._store is not None:
            self._store.save_task(task.task_id, task.spec, task.options, "queued")
        logger.info("Задание %s поставлено в очередь", task.task_id)
        QTimer.singleShot(0, self._start_next)
        return task.task_id

    def get_status(self, task_id: str) -> Optional[dict[str, Any]]:
        """Возвращает статус задания (формат ``StatusMsg``) или ``None``.

        Args:
            task_id: идентификатор задания.
        """
        task = self._tasks.get(task_id)
        if task is not None:
            return task.to_status_dict(self._eta_for_status(task))
        if self._store is not None:
            row = self._store.load_task(task_id)
            if row is not None:
                return {
                    "type": "status",
                    "task_id": task_id,
                    "state": row["state"],
                    "current_site": (row.get("progress") or {}).get("site"),
                    "progress": row.get("progress"),
                    "eta_seconds": None,
                    "errors": None,
                }
        return None

    def get_task(self, task_id: str) -> Optional[Task]:
        """Возвращает объект :class:`Task` (для тестов и GUI 3.5)."""
        return self._tasks.get(task_id)

    def stop(self, task_id: str) -> bool:
        """Корректно останавливает выполняющееся задание (S-12).

        ``running → stopping``; рабочий поток прерывает паузу, собранное
        сохраняется, затем испускается ``task_stopped``.

        Args:
            task_id: идентификатор задания.

        Returns:
            bool: ``True`` — остановка запрошена; ``False`` — задание
            неизвестно или не выполняется (для задания в очереди
            используйте :meth:`cancel`).
        """
        task = self._tasks.get(task_id)
        if task is None or task.state != "running":
            return False
        task.stopped_by_user = True
        self._set_state(task, "stopping")
        task.stop_event.set()
        logger.info("Задание %s: запрошена остановка", task_id)
        return True

    def cancel(self, task_id: str) -> bool:
        """Отменяет задание.

        В очереди — немедленно: ``cancelled`` и ``task_failed(id,
        "cancelled")``. Выполняющееся — останавливается, после чего
        получает ``cancelled`` и тот же сигнал.

        Args:
            task_id: идентификатор задания.

        Returns:
            bool: ``True``, если отмена принята.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.state == "queued":
            try:
                self._queue.remove(task_id)
            except ValueError:
                pass
            self._finalize(task, "cancelled")
            self.task_failed.emit(task_id, "cancelled")
            return True
        if task.state in ("running", "stopping"):
            task.cancel_requested = True
            if task.state == "running":
                self._set_state(task, "stopping")
            task.stop_event.set()
            return True
        return False

    def shutdown(self) -> None:
        """Останавливает текущее задание и очищает очередь.

        Сигналы не испускаются. Записи в ``StateStore`` не финализируются:
        прерванное и ожидавшие задания остаются «активными» и могут быть
        восстановлены при следующем запуске (S-07). После вызова
        :meth:`submit` недоступен.
        """
        self._shutting_down = True
        self._queue.clear()
        current = self._tasks.get(self._current_id) if self._current_id else None
        if current is not None:
            current.stop_event.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(SHUTDOWN_JOIN_TIMEOUT_SEC)
        self._worker = None
        self._current_id = None
        logger.info("TaskManager остановлен")

    def queue_length(self) -> int:
        """Число заданий, ожидающих в очереди."""
        return len(self._queue)

    def current_task_id(self) -> Optional[str]:
        """Идентификатор выполняющегося задания или ``None``."""
        return self._current_id

    # ------------------------------------------------------------------
    # Параметры задания
    # ------------------------------------------------------------------
    @staticmethod
    def _sites(task: Task) -> list[str]:
        """Площадки задания (по умолчанию — без ``sberauto.com``)."""
        sites = task.options.get("sites")
        return list(sites) if sites else list(protocol.DEFAULT_SITES)

    @staticmethod
    def _max_ads_per_site(task: Task) -> int:
        """Лимит объявлений на площадку (S-17)."""
        return int(
            task.options.get("max_ads_per_site") or protocol.DEFAULT_MAX_ADS_PER_SITE
        )

    @classmethod
    def _pages_per_site(cls, task: Task) -> int:
        """Число страниц на площадку с учётом лимита объявлений.

        Выбор усечения (3.4a): если ``pages × 20 > max_ads_per_site``,
        **усекается число страниц** до ``ceil(max_ads / 20)``; последняя
        страница при этом может содержать меньше 20 объявлений, так что
        лимит на площадку не превышается.
        """
        pages = int(
            task.options.get("max_pages_per_site")
            or protocol.DEFAULT_MAX_PAGES_PER_SITE
        )
        by_ads = math.ceil(cls._max_ads_per_site(task) / STUB_ADS_PER_PAGE)
        return max(1, min(pages, by_ads))

    @staticmethod
    def _page_delay_range(task: Task) -> tuple[float, float]:
        """Диапазон паузы между запросами к одной площадке, сек.

        * ``test_fast_mode`` + ``test_delay`` (число или ``[min, max]``) —
          заданное значение;
        * ``test_fast_mode`` без ``test_delay`` — ``(0.1, 0.3)``;
        * иначе — ``DEFAULT_SERVER_PAGE_DELAY_SEC`` (120–300 с).
        """
        if task.options.get("test_fast_mode"):
            raw = task.options.get("test_delay")
            try:
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    low = high = float(raw)
                elif isinstance(raw, (list, tuple)) and len(raw) == 2:
                    low, high = float(raw[0]), float(raw[1])
                else:
                    return FAST_PAGE_DELAY_SEC
            except (TypeError, ValueError):
                return FAST_PAGE_DELAY_SEC
            if low < 0 or high < low:
                return FAST_PAGE_DELAY_SEC
            return (low, high)
        low, high = constants.DEFAULT_SERVER_PAGE_DELAY_SEC
        return (float(low), float(high))

    # ------------------------------------------------------------------
    # ETA
    # ------------------------------------------------------------------
    def _estimate_eta(self, task: Task) -> int:
        """Оценивает оставшееся время, сек (формулы ТЗ 3.4a rev 2, п. 3.5).

        * пока выполнено меньше ``N`` запросов (или нет данных о скорости) —
          ``remaining × avg_delay / N``;
        * далее — по фактической скорости ``remaining / (completed/elapsed)``.

        ``elapsed`` считается по ``time.monotonic()`` (а не по системным
        часам), чтобы смена времени не «уводила» ETA.
        """
        avg_delay = sum(self._page_delay_range(task)) / 2
        sites = self._sites(task)
        total = len(sites) * self._pages_per_site(task)
        with task.lock:
            completed = sum(int(s["pages_done"]) for s in task.per_site.values())
            started = task.started_monotonic
        remaining = max(0, total - completed)
        planned = int(remaining * avg_delay / len(sites))

        if completed < len(sites) or started is None:
            return planned
        elapsed = time.monotonic() - started
        if elapsed <= 0 or completed == 0:
            return planned
        return int(remaining / (completed / elapsed))

    def _eta_for_status(self, task: Task) -> Optional[int]:
        """ETA для ``get_status``: 0 для завершённых/отменённых заданий."""
        if task.state in ("completed", "failed", "cancelled"):
            return 0
        return self._estimate_eta(task)

    # ------------------------------------------------------------------
    # Жизненный цикл (поток менеджера)
    # ------------------------------------------------------------------
    def _set_state(self, task: Task, state: str) -> None:
        """Меняет состояние в памяти и в ``StateStore``."""
        task.state = state
        if self._store is not None:
            self._store.update_task_state(task.task_id, state)

    def _finalize(self, task: Task, state: str) -> None:
        """Переводит задание в конечное состояние и проставляет время."""
        task.finished_at = _utc_now_iso()
        self._set_state(task, state)

    def _start_next(self) -> None:
        """Запускает следующее задание из очереди, если никто не выполняется."""
        if self._shutting_down or self._current_id is not None:
            return
        while self._queue:
            task = self._tasks.get(self._queue.popleft())
            if task is None or task.state != "queued":
                continue
            task.started_at = _utc_now_iso()
            task.started_monotonic = time.monotonic()
            self._current_id = task.task_id
            self._set_state(task, "running")
            self.task_started.emit(task.task_id)
            self._worker = threading.Thread(
                target=self._process_task,
                args=(task,),
                name=f"aprice-task-{task.task_id[:8]}",
                daemon=True,
            )
            self._worker.start()
            return

    def _on_progress(self, task_id: str, progress: dict) -> None:
        """Слот (поток менеджера): сохраняет прогресс и ретранслирует сигнал."""
        task = self._tasks.get(task_id)
        if task is None or self._shutting_down:
            return
        if self._store is not None:
            with task.lock:
                snapshot = {**progress, "per_site": {
                    s: dict(v) for s, v in task.per_site.items()
                }}
            self._store.save_progress(task_id, snapshot)
        self.task_progress.emit(task_id, progress)

    def _on_finished(self, task_id: str) -> None:
        """Слот (поток менеджера): финализация задания и запуск следующего."""
        task = self._tasks.get(task_id)
        worker = self._worker
        if worker is not None:
            worker.join()
        self._worker = None
        self._current_id = None
        if task is None or self._shutting_down:
            return

        total = task.total_ads
        if task.failure_reason is not None:
            self._finalize(task, "failed")
            self.task_failed.emit(task_id, task.failure_reason)
        elif task.cancel_requested:
            self._finalize(task, "cancelled")
            self.task_failed.emit(task_id, "cancelled")
        elif task.stopped_by_user:
            # Собранное сохранено (S-12) -> completed + сигнал task_stopped.
            self._finalize(task, "completed")
            self.task_stopped.emit(task_id, total)
        else:
            self._finalize(task, "completed")
            self.task_completed.emit(task_id, total)
        logger.info("Задание %s завершено: state=%s, ads=%d", task_id, task.state, total)
        self._start_next()

    # ------------------------------------------------------------------
    # Планировщик (рабочий поток)
    # ------------------------------------------------------------------
    def _process_task(self, task: Task) -> None:
        """Round-robin планировщик сбора (выполняется в рабочем потоке).

        Первые запросы площадок разнесены на ``avg_delay / N``; затем
        каждая площадка получает следующий запрос через
        ``random.uniform(*page_delay_range)`` после предыдущего.
        Страницы обходятся «слоями»: A1 B1 C1, A2 B2 C2, ...

        Рабочий поток не обращается к БД и не шлёт публичные сигналы —
        только ``_sig_progress`` / ``_sig_finished`` (queued).
        """
        try:
            sites = self._sites(task)
            pages_per_site = self._pages_per_site(task)
            max_ads = self._max_ads_per_site(task)
            delay_range = self._page_delay_range(task)
            avg_delay = sum(delay_range) / 2

            start = time.monotonic()
            last_request_at = {
                site: start + i * avg_delay / len(sites)
                for i, site in enumerate(sites)
            }

            for page in range(1, pages_per_site + 1):
                for site in sites:
                    if task.stop_event.is_set():
                        return
                    wait = last_request_at[site] - time.monotonic()
                    if wait > 0 and not interruptible_sleep(task.stop_event, wait):
                        return  # stop_event установлен во время паузы
                    if task.stop_event.is_set():
                        return

                    # «Запрос»: в 3.4a — мгновенная заглушка без Ad.
                    with task.lock:
                        stats = task.per_site.setdefault(
                            site, {"pages_done": 0, "ads": 0}
                        )
                        ads_this_page = min(
                            STUB_ADS_PER_PAGE, max(0, max_ads - stats["ads"])
                        )
                        stats["pages_done"] += 1
                        stats["ads"] += ads_this_page
                        total_ads = sum(s["ads"] for s in task.per_site.values())
                        task.progress = {
                            "site": site,
                            "page": page,
                            "ads_collected": total_ads,
                        }
                        progress = dict(task.progress)
                        task.request_log.append((site, page, time.monotonic()))

                    last_request_at[site] = time.monotonic() + random.uniform(
                        *delay_range
                    )
                    self._sig_progress.emit(task.task_id, progress)
        except Exception as exc:  # noqa: BLE001 - любая ошибка -> failed
            logger.exception("Задание %s: ошибка планировщика", task.task_id)
            with task.lock:
                task.errors.append({"type": type(exc).__name__, "message": str(exc)})
            task.failure_reason = f"{type(exc).__name__}: {exc}"
        finally:
            self._sig_finished.emit(task.task_id)
