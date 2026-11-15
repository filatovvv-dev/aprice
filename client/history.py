"""История запросов APrice-Client (подзадача 4.2). Модуль без Qt.

Хранит последние :data:`MAX_ENTRIES` оценок в ``data/aprice_client_history.json``:
только ``spec`` и рассчитанные показатели, **не** сами объявления (файл не растёт).

Формат записи::

    {"timestamp": "2026-11-15T10:30:00", "spec": {...}, "count": 564, "min": ...,
     "max": ..., "mean": ..., "median": ..., "trimmed_mean": ..., "warning": null}

(поле ``warning`` добавлено к формату ТЗ, чтобы просмотр из истории показывал то же
предупреждение, что и исходная оценка).

Файл повреждён или недоступен -> история пуста, приложение не падает. Запись
атомарная (временный файл + ``os.replace``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from common import constants

logger = logging.getLogger("aprice.client.history")

#: Сколько записей хранится (старшие вытесняются).
MAX_ENTRIES = 10
#: Показатели, которые копируются из статистики в запись.
STAT_KEYS = ("count", "min", "max", "mean", "median", "trimmed_mean", "warning")

PathLike = Union[Path, str]


def _history_path(path: Optional[PathLike]) -> Path:
    return Path(path) if path is not None else constants.CLIENT_HISTORY_FILE


def _read(path: Path) -> list[dict[str, Any]]:
    """Читает файл как список записей в порядке хранения; ошибки -> пустой список."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:  # ValueError — повреждённый JSON
        logger.warning("История запросов не прочитана (%s): %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("История запросов: ожидался список, получено %s", type(data).__name__)
        return []
    return [item for item in data if isinstance(item, dict)]


def load_history(path: Optional[PathLike] = None) -> list[dict[str, Any]]:
    """Возвращает записи, **новые первыми** (сортировка по ``timestamp``).

    Записи с одинаковым временем идут в порядке «позже добавленные — раньше».
    """
    entries = _read(_history_path(path))
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda pair: (str(pair[1].get("timestamp", "")), pair[0]), reverse=True)
    return [entry for _, entry in indexed]


def save_to_history(
    stats: dict[str, Any],
    spec: dict[str, Any],
    path: Optional[PathLike] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Добавляет запись и оставляет только последние :data:`MAX_ENTRIES`.

    Args:
        stats: результат :func:`client.stats.calculate_stats`.
        spec: ``spec`` задания (как он ушёл на Server).
        path: файл истории (по умолчанию ``data/aprice_client_history.json``).
        now: время записи (для тестов); по умолчанию — текущее локальное.

    Returns:
        dict: добавленная запись.

    Raises:
        OSError: не удалось записать файл.
    """
    target = _history_path(path)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    entry: dict[str, Any] = {"timestamp": stamp, "spec": dict(spec)}
    entry.update({key: stats.get(key) for key in STAT_KEYS})

    entries = _read(target)
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return entry


def describe(entry: dict[str, Any]) -> str:
    """Подпись записи для списка: ``15.11.2026 10:30 — BMW X5, 2018–2021 (564 объявл.)``."""
    spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}
    try:
        when = datetime.fromisoformat(str(entry.get("timestamp"))).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        when = "—"
    car = " ".join(str(spec.get(k)) for k in ("manufacturer", "model") if spec.get(k)) or "—"
    year_from, year_to = spec.get("year_from"), spec.get("year_to")
    if year_from and year_to:
        years = str(year_from) if year_from == year_to else f"{year_from}–{year_to}"
        car = f"{car}, {years}"
    return f"{when} — {car} ({entry.get('count', 0)} объявл.)"
