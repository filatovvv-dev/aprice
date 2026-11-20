"""Пауза площадки до решения CAPTCHA пользователем (подзадача 5.7).

Раньше при ``CaptchaDetectedError`` Server помечал площадку как отказавшую и шёл дальше —
пользователь не мог решить капчу и продолжить. Теперь рабочий поток **приостанавливается**
на этой площадке (``wait``), а пользователь выбирает в диалоге:

* «Я решил, продолжить» → :meth:`CaptchaGate.resume` — поток ждёт несколько секунд и
  повторяет **ту же** страницу;
* «Пропустить сайт» (или закрытие диалога) → :meth:`CaptchaGate.skip` — площадка
  пропускается, сбор идёт дальше;
* остановка/отмена задания → ожидание прерывается.

Модуль не зависит от Qt: им пользуются рабочий поток (ждёт) и поток GUI (решает).
"""

from __future__ import annotations

import threading
from typing import Optional

RESUME = "resume"
SKIP = "skip"

#: Пауза после «Я решил, продолжить» перед повтором страницы, сек.
RESUME_DELAY_SEC = 5.0
#: Как часто ожидающий поток проверяет ``stop_event``, сек.
POLL_SEC = 0.1


class _Entry:
    __slots__ = ("url", "event", "decision")

    def __init__(self, url: str) -> None:
        self.url = url
        self.event = threading.Event()
        self.decision: Optional[str] = None


class CaptchaGate:
    """Потокобезопасная таблица ожидающих CAPTCHA: ``(task_id, site) -> решение``."""

    def __init__(self) -> None:
        """Создаёт пустую таблицу."""
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}

    # ------------------------------------------------------------------
    # Рабочий поток
    # ------------------------------------------------------------------
    def open(self, task_id: str, site: str, url: str) -> None:
        """Регистрирует ожидание (вызывается до показа диалога, чтобы не потерять решение)."""
        with self._lock:
            self._entries[(task_id, site)] = _Entry(url)

    def wait(self, task_id: str, site: str, stop_event: threading.Event) -> Optional[str]:
        """Блокирует поток до решения пользователя.

        Returns:
            ``RESUME`` / ``SKIP`` либо ``None``, если задание остановлено (``stop_event``).
        """
        with self._lock:
            entry = self._entries.get((task_id, site))
        if entry is None:
            return SKIP
        try:
            while not entry.event.wait(POLL_SEC):
                if stop_event.is_set():
                    return None
            return entry.decision
        finally:
            with self._lock:
                if self._entries.get((task_id, site)) is entry:
                    del self._entries[(task_id, site)]

    # ------------------------------------------------------------------
    # Поток GUI
    # ------------------------------------------------------------------
    def _decide(self, decision: str, site: str, task_id: Optional[str]) -> bool:
        with self._lock:
            for (tid, s), entry in self._entries.items():
                if s == site and (task_id is None or tid == task_id) and not entry.event.is_set():
                    entry.decision = decision
                    entry.event.set()
                    return True
        return False

    def resume(self, site: str, task_id: Optional[str] = None) -> bool:
        """«Я решил, продолжить»: ``True``, если нашлось ожидающее решение для площадки."""
        return self._decide(RESUME, site, task_id)

    def skip(self, site: str, task_id: Optional[str] = None) -> bool:
        """«Пропустить сайт»: ``True``, если нашлось ожидающее решение для площадки."""
        return self._decide(SKIP, site, task_id)

    def waiting(self, task_id: Optional[str] = None) -> dict[str, str]:
        """Ожидающие решения: ``{site: url}`` (для задания ``task_id`` или всех)."""
        with self._lock:
            return {
                s: e.url
                for (tid, s), e in self._entries.items()
                if (task_id is None or tid == task_id) and not e.event.is_set()
            }

    def release_task(self, task_id: str) -> None:
        """Снимает все ожидания задания (задание завершено/остановлено): потоки получают SKIP."""
        with self._lock:
            for (tid, _s), entry in self._entries.items():
                if tid == task_id and not entry.event.is_set():
                    entry.decision = SKIP
                    entry.event.set()
