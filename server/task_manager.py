"""TaskManager APrice-Server: FSM задания, очередь, round-robin, ETA (3.4a rev 2).

Модуль содержит:

* :class:`Task` — состояние одного задания (в т.ч. ``per_site`` для ETA);
* :class:`TaskManager` — очередь (одно задание одновременно), FSM и
  планировщик сбора.

Планировщик — **round-robin**: запросы к разным площадкам чередуются,
первые запросы площадок разнесены на ``avg_delay / N``, а каждая
площадка видит запрос строго раз в ``page_delay`` (per-site pacing).
«Запрос» (3.4b) — вызов :meth:`BaseSiteParser.fetch_page` парсера
площадки; объявления сохраняются в ``StateStore`` после каждой страницы
(S-06), ошибки площадок (CAPTCHA, блокировка, неподдерживаемая площадка)
не прерывают задание, а приостанавливают только свою площадку (S-13/S-14).
После перезапуска :meth:`TaskManager.restore` возвращает активные
задания в очередь и продолжает с последней страницы (S-07).

Потоки
------
Планировщик (:meth:`TaskManager._process_task`) выполняется в рабочем
потоке: он спит через :func:`common.utils.interruptible_sleep` и потому
не должен блокировать GUI. ``StateStore`` однопоточный
(``check_same_thread=True``), поэтому рабочий поток **не** обращается к
БД и не шлёт публичные сигналы: он испускает внутренние сигналы
(``_internal_save_ads``, ``_internal_save_progress``,
``_internal_update_state``, ``_sig_progress``, ``_sig_captcha``,
``_sig_finished``) с ``QueuedConnection``, а запись в БД и публичные
сигналы выполняются в потоке, где живёт ``TaskManager`` (для этого в нём
должен работать цикл событий Qt). Порядок событий одного потока сохраняется:
объявления страницы записываются раньше её прогресса.

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
from common.config import Config
from common.utils import interruptible_sleep
from server import protocol
from server.parsers import get_parser
from server.parsers.base import (
    Ad,
    BaseSiteParser,
    BlockedError,
    CaptchaDetectedError,
    UnsupportedSiteError,
)
from server.state import StateStore

logger = logging.getLogger("aprice.server.task_manager")

#: Нижняя граница размера страницы выдачи (объявлений за «запрос»): по ней
#: считается верхняя оценка числа страниц под лимит ``max_ads_per_site``.
STUB_ADS_PER_PAGE: int = 20

#: Верхняя граница размера страницы — для грубой оценки ``pages_done`` по
#: числу сохранённых объявлений (не пропустить несобранные страницы).
MAX_ADS_PER_PAGE_ESTIMATE: int = 50

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
    #: Площадки, приостановленные из-за ошибки: ``{site: kind}`` (S-13/S-14).
    failed_sites: dict[str, str] = field(default_factory=dict, repr=False)
    #: Задание восстановлено после перезапуска (перед стартом — пауза).
    restored: bool = field(default=False, repr=False)
    #: Сколько страниц было выполнено к моменту старта (для ETA при restore).
    pages_at_start: int = field(default=0, repr=False)

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

    # Внутренние сигналы: любой поток -> поток менеджера (всегда queued).
    # Запись в StateStore идёт только через них (SQLite однопоточный).
    _internal_save_ads = pyqtSignal(str, str, list)      # task_id, site, ads
    _internal_save_progress = pyqtSignal(str, dict)      # task_id, progress
    _internal_update_state = pyqtSignal(str, str)        # task_id, state
    _sig_progress = pyqtSignal(str, dict)                # ретрансляция в public
    _sig_captcha = pyqtSignal(str, str, str)             # task_id, site, url
    _sig_finished = pyqtSignal(str)

    def __init__(
        self,
        config: Config,
        state_store: Optional[StateStore] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """Инициализирует менеджер.

        Args:
            config: конфигурация приложения (передаётся парсерам).
            state_store: хранилище состояния (``None`` — без персистентности,
                удобно для тестов планировщика). Создаётся в потоке
                менеджера и используется только из него.
            parent: родительский QObject.
        """
        super().__init__(parent)
        self._config = config
        self._store = state_store
        self._parsers: dict[str, BaseSiteParser] = {}  # site -> парсер (кэш)
        self._parsers_lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._queue: deque[str] = deque()
        self._current_id: Optional[str] = None
        self._worker: Optional[threading.Thread] = None
        self._shutting_down = False
        self._owner_ident = threading.get_ident()
        queued = Qt.ConnectionType.QueuedConnection
        self._internal_save_ads.connect(self._on_save_ads, queued)
        self._internal_save_progress.connect(self._on_save_progress, queued)
        self._internal_update_state.connect(self._on_update_state, queued)
        self._sig_progress.connect(self._on_progress, queued)
        self._sig_captcha.connect(self._on_captcha, queued)
        self._sig_finished.connect(self._on_finished, queued)

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
                служебные ``test_fast_mode`` (только короткие паузы), ``test_delay``
                и ``use_stub_data`` (заглушка вместо сети; для автотестов).

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

        Задание в очереди отменяется сразу: ``cancelled`` и
        ``task_stopped(task_id, 0)``.

        Returns:
            bool: ``True`` — остановка/отмена принята; ``False`` — задание
            неизвестно или уже завершается.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.state == "queued":
            # Пользователь не должен знать, в каком состоянии задание:
            # «Остановить» для ожидающего задания равно отмене (3.4b).
            try:
                self._queue.remove(task_id)
            except ValueError:
                pass
            task.stopped_by_user = True
            self._finalize(task, "cancelled")
            self.task_stopped.emit(task_id, 0)
            return True
        if task.state != "running":
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

    def page_totals(self, task_id: str) -> Optional[tuple[int, int]]:
        """Возвращает ``(выполнено_страниц, всего_страниц)`` по всем площадкам.

        Нужен окну прогресса (PW-08) для определённого прогресс-бара; в
        контракт ``progress``/``StatusMsg`` эти числа не входят.

        Args:
            task_id: идентификатор задания.

        Returns:
            tuple | None: ``None``, если задание неизвестно.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        total = len(self._sites(task)) * self._pages_per_site(task)
        with task.lock:
            done = sum(int(s["pages_done"]) for s in task.per_site.values())
        return min(done, total), total

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
            baseline = task.pages_at_start
        remaining = max(0, total - completed)
        planned = int(remaining * avg_delay / len(sites))

        # Скорость считаем только по страницам, выполненным после (пере)старта:
        # страницы, восстановленные из БД, «бесплатные» и исказили бы ETA.
        done_now = completed - baseline
        if done_now < len(sites) or started is None:
            return planned
        elapsed = time.monotonic() - started
        if elapsed <= 0 or done_now == 0:
            return planned
        return int(remaining / (done_now / elapsed))

    def _eta_for_status(self, task: Task) -> Optional[int]:
        """ETA для ``get_status``: 0 для завершённых/отменённых заданий."""
        if task.state in ("completed", "failed", "cancelled"):
            return 0
        return self._estimate_eta(task)

    # ------------------------------------------------------------------
    # Жизненный цикл (поток менеджера)
    # ------------------------------------------------------------------
    def _set_state(self, task: Task, state: str) -> None:
        """Меняет состояние в памяти и в ``StateStore``.

        Из потока менеджера пишет в БД напрямую; из любого другого —
        через queued-сигнал ``_internal_update_state`` (SQLite однопоточный).
        """
        task.state = state
        if self._store is None:
            return
        if threading.get_ident() == self._owner_ident:
            self._on_update_state(task.task_id, state)
        else:
            self._internal_update_state.emit(task.task_id, state)

    # --- слоты записи в БД (всегда выполняются в потоке менеджера) -----
    def _on_save_ads(self, task_id: str, site: str, ads: list) -> None:
        """Слот: сохраняет объявления страницы (S-06, дедупликация по PK)."""
        if self._store is None:
            return
        try:
            self._store.save_ads(task_id, site, ads)
        except Exception:  # noqa: BLE001 - слот не должен ронять приложение
            logger.exception("Задание %s: не удалось сохранить объявления", task_id)

    def _on_save_progress(self, task_id: str, progress: dict) -> None:
        """Слот: сохраняет прогресс (вместе с ``per_site``) для восстановления."""
        if self._store is None:
            return
        try:
            self._store.save_progress(task_id, progress)
        except Exception:  # noqa: BLE001
            logger.exception("Задание %s: не удалось сохранить прогресс", task_id)

    def _on_update_state(self, task_id: str, state: str) -> None:
        """Слот: записывает состояние задания."""
        if self._store is None:
            return
        try:
            self._store.update_task_state(task_id, state)
        except Exception:  # noqa: BLE001
            logger.exception("Задание %s: не удалось сохранить состояние", task_id)

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
            task.pages_at_start = sum(
                int(v["pages_done"]) for v in task.per_site.values()
            )
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
        """Слот (поток менеджера): ретранслирует ``task_progress``."""
        if task_id not in self._tasks or self._shutting_down:
            return
        self.task_progress.emit(task_id, progress)

    def _on_captcha(self, task_id: str, site: str, url: str) -> None:
        """Слот (поток менеджера): ретранслирует ``captcha_required``."""
        if task_id not in self._tasks or self._shutting_down:
            return
        self.captcha_required.emit(task_id, site, url)

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
        if task.cancel_requested:
            self._finalize(task, "cancelled")
            self.task_failed.emit(task_id, "cancelled")
        elif task.stopped_by_user:
            # Собранное сохранено (S-12) -> completed + сигнал task_stopped.
            self._finalize(task, "completed")
            self.task_stopped.emit(task_id, total)
        elif task.failure_reason is None and self._all_sites_failed(task, total):
            task.failure_reason = "все площадки недоступны: " + ", ".join(
                f"{s}={k}" for s, k in task.failed_sites.items()
            )
            self._finalize(task, "failed")
            self.task_failed.emit(task_id, task.failure_reason)
        elif task.failure_reason is not None:
            self._finalize(task, "failed")
            self.task_failed.emit(task_id, task.failure_reason)
        else:
            self._finalize(task, "completed")
            self.task_completed.emit(task_id, total)
        logger.info("Задание %s завершено: state=%s, ads=%d", task_id, task.state, total)
        self._start_next()

    def _all_sites_failed(self, task: Task, total_ads: int) -> bool:
        """``True``, если ни одна площадка не дала данных (все приостановлены)."""
        sites = self._sites(task)
        return (
            total_ads == 0
            and bool(sites)
            and all(site in task.failed_sites for site in sites)
        )

    # ------------------------------------------------------------------
    # Восстановление после перезапуска (S-07)
    # ------------------------------------------------------------------
    def restore(self) -> list[str]:
        """Восстанавливает активные задания из ``StateStore`` после запуска.

        * ``running`` → снова ``queued``: сбор продолжится с последней
          страницы (``per_site`` восстанавливается из сохранённого прогресса);
          перед первым запросом — «человеческая» пауза;
        * ``queued`` → в очередь;
        * ``stopping`` → сразу ``cancelled`` и ``task_stopped(id, n)``, где
          ``n`` — число уже сохранённых объявлений.

        Returns:
            list[str]: идентификаторы заданий, поставленных в очередь.
        """
        if self._store is None or self._shutting_down:
            return []
        restored: list[str] = []
        for row in self._store.list_active_tasks():
            task_id = row["task_id"]
            if task_id in self._tasks:
                continue
            task = Task(
                task_id=task_id,
                spec=row["spec"],
                options=row["options"],
                created_at=row.get("created_at") or _utc_now_iso(),
            )
            task.per_site = self._restore_per_site(task, row.get("progress"))
            if row.get("progress"):
                task.progress = {
                    k: row["progress"][k]
                    for k in ("site", "page", "ads_collected")
                    if k in row["progress"]
                }
            self._tasks[task_id] = task

            if row["state"] == "stopping":
                self._finalize(task, "cancelled")
                self.task_stopped.emit(task_id, task.total_ads)
                continue
            if row["state"] == "running":
                task.restored = True
                self._set_state(task, "queued")
            self._queue.append(task_id)
            restored.append(task_id)
            logger.info("Задание %s восстановлено (%s)", task_id, row["state"])
        if restored:
            QTimer.singleShot(0, self._start_next)
        return restored

    def _restore_per_site(
        self, task: Task, progress: Optional[dict]
    ) -> dict[str, dict]:
        """Восстанавливает ``per_site`` (``pages_done`` и ``ads``) по БД.

        Приоритет источников ``pages_done``:

        1. ``progress["per_site"]`` — точный снимок;
        2. ``progress["site"]`` / ``progress["page"]`` — вывод из порядка
           round-robin: площадки не позже последней обработанной прошли
           ``page`` страниц, остальные — ``page - 1``;
        3. грубая оценка ``count_ads // 50`` (нижняя граница числа страниц —
           лучше повторить страницу, чем пропустить: дубликаты отсекает PK).

        ``ads`` берётся из БД (``count_ads``) — это единственный источник
        правды о сохранённом.
        """
        assert self._store is not None
        sites = self._sites(task)
        progress = progress or {}
        saved = progress.get("per_site")
        last_site, last_page = progress.get("site"), progress.get("page")
        result: dict[str, dict] = {}
        for index, site in enumerate(sites):
            ads = self._store.count_ads(task.task_id, site)
            if isinstance(saved, dict) and isinstance(saved.get(site), dict):
                pages = int(saved[site].get("pages_done", 0))
            elif last_site in sites and isinstance(last_page, int):
                pages = last_page if index <= sites.index(last_site) else last_page - 1
            else:
                pages = ads // MAX_ADS_PER_PAGE_ESTIMATE
            pages = max(0, pages)
            if pages or ads:
                result[site] = {"pages_done": pages, "ads": ads}
        return result

    # ------------------------------------------------------------------
    # Планировщик (рабочий поток)
    # ------------------------------------------------------------------
    def _get_parser(self, site: str) -> BaseSiteParser:
        """Возвращает (с кэшированием) парсер площадки.

        Raises:
            UnsupportedSiteError: площадка не поддерживается (например,
                ``sberauto.com`` — S-15).
        """
        with self._parsers_lock:
            parser = self._parsers.get(site)
            if parser is None:
                parser = get_parser(site, self._config)
                self._parsers[site] = parser
            return parser

    def _record_failure(
        self,
        task: Task,
        site: str,
        kind: str,
        message: str,
        url: Optional[str] = None,
    ) -> None:
        """Фиксирует ошибку площадки и приостанавливает **только её**.

        Задание продолжается с остальными площадками (S-13/S-14). Для
        CAPTCHA дополнительно уведомляет клиента (``captcha_required``).
        """
        with task.lock:
            task.errors.append(
                {"site": site, "kind": kind, "ts": _utc_now_iso(), "message": message}
            )
            task.failed_sites[site] = kind
        logger.warning("Задание %s: %s на %s (%s)", task.task_id, kind, site, message)
        if kind == "captcha":
            self._sig_captcha.emit(task.task_id, site, url or "")

    def _on_page_collected(
        self, task: Task, site: str, page: int, ads: list[Ad]
    ) -> dict:
        """Учитывает страницу: лимит, ``per_site``, прогресс; шлёт запись в БД.

        Выполняется в рабочем потоке; в БД пишет только через queued-сигналы.
        Объявления сверх ``max_ads_per_site`` отбрасываются.

        Returns:
            dict: прогресс ``{"site", "page", "ads_collected"}``.
        """
        max_ads = self._max_ads_per_site(task)
        with task.lock:
            stats = task.per_site.setdefault(site, {"pages_done": 0, "ads": 0})
            accepted = ads[: max(0, max_ads - stats["ads"])]
            stats["pages_done"] = max(stats["pages_done"], page)
            stats["ads"] += len(accepted)
            task.collected.setdefault(site, []).extend(accepted)
            total_ads = sum(v["ads"] for v in task.per_site.values())
            task.progress = {"site": site, "page": page, "ads_collected": total_ads}
            progress = dict(task.progress)
            snapshot = {
                **progress,
                "per_site": {s: dict(v) for s, v in task.per_site.items()},
            }
            task.request_log.append((site, page, time.monotonic()))
        if accepted:
            self._internal_save_ads.emit(
                task.task_id, site, [ad.to_dict() for ad in accepted]
            )
        self._internal_save_progress.emit(task.task_id, snapshot)
        return progress

    def _process_task(self, task: Task) -> None:
        """Round-robin планировщик сбора (выполняется в рабочем потоке).

        Первые запросы площадок разнесены на ``avg_delay / N``; затем
        каждая площадка получает следующий запрос через
        ``random.uniform(*page_delay_range)`` после предыдущего (график
        адаптивен: отсчёт идёт от фактического окончания запроса).
        Страницы обходятся «слоями»: A1 B1 C1, A2 B2 C2, ...

        * Страницы, уже собранные до перезапуска (``per_site``), пропускаются.
        * Площадка, давшая CAPTCHA / блокировку / ошибку, приостанавливается
          до конца задания, остальные продолжают работать.
        * Рабочий поток не обращается к БД и не шлёт публичные сигналы.
        """
        try:
            sites = self._sites(task)
            pages_per_site = self._pages_per_site(task)
            max_ads = self._max_ads_per_site(task)
            delay_range = self._page_delay_range(task)
            avg_delay = sum(delay_range) / 2

            if task.restored:
                # Эмуляция «человек вернулся к компьютеру» (после перезапуска).
                low, high = (
                    delay_range
                    if task.options.get("test_fast_mode")
                    else constants.DEFAULT_SERVER_SITE_DELAY_SEC
                )
                if not interruptible_sleep(
                    task.stop_event, random.uniform(float(low), float(high))
                ):
                    return
                with task.lock:
                    task.started_monotonic = time.monotonic()

            start = time.monotonic()
            last_request_at = {
                site: start + i * avg_delay / len(sites)
                for i, site in enumerate(sites)
            }

            for page in range(1, pages_per_site + 1):
                for site in sites:
                    if task.stop_event.is_set():
                        return
                    if all(s in task.failed_sites for s in sites):
                        return  # работать не с чем -> итог в _on_finished
                    if site in task.failed_sites:
                        continue
                    with task.lock:
                        stats = task.per_site.get(site, {"pages_done": 0, "ads": 0})
                        already_done = stats["pages_done"] >= page
                        limit_reached = stats["ads"] >= max_ads
                    if already_done or limit_reached:
                        continue  # собрано до перезапуска / лимит площадки

                    try:
                        parser = self._get_parser(site)
                    except UnsupportedSiteError as exc:
                        self._record_failure(task, site, "unsupported", str(exc))
                        continue

                    wait = last_request_at[site] - time.monotonic()
                    if wait > 0 and not interruptible_sleep(task.stop_event, wait):
                        return  # stop_event установлен во время паузы
                    if task.stop_event.is_set():
                        return

                    try:
                        ads = parser.fetch_page(
                            task.spec, dict(task.options), page, task.stop_event
                        )
                    except CaptchaDetectedError as exc:
                        self._record_failure(task, site, "captcha", str(exc), exc.url)
                        continue
                    except BlockedError as exc:
                        self._record_failure(task, site, "blocked", str(exc))
                        continue
                    except UnsupportedSiteError as exc:
                        self._record_failure(task, site, "unsupported", str(exc))
                        continue
                    except Exception as exc:  # noqa: BLE001 - ошибка парсера
                        logger.exception("[%s] ошибка парсера", site)
                        self._record_failure(
                            task, site, "error", f"{type(exc).__name__}: {exc}"
                        )
                        continue

                    if task.stop_event.is_set() and not ads:
                        return  # остановка во время запроса, страницы нет
                    progress = self._on_page_collected(task, site, page, ads)
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
