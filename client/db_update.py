"""Обновление справочника ``aprice.db`` из зеркала (подзадача 4.4). Без Qt.

:func:`download_database` скачивает файл по ``db_update_url``, проверяет и только
затем подменяет рабочую базу:

1. скачивание во временный файл рядом с целевым (``aprice.db.download``);
2. сигнатура SQLite (HTML-страница ошибки вместо базы отсеивается сразу);
3. ``PRAGMA integrity_check`` == ``ok`` и наличие таблиц справочника с данными;
4. резервная копия ``aprice.db.bak`` (перезаписывается);
5. атомарная подмена ``os.replace``.

При любой ошибке рабочая база остаётся нетронутой, временный файл удаляется, а
наружу выходит :class:`DbUpdateError` с понятным текстом. Публичные ссылки
Яндекс.Диска (``disk.yandex.*``, ``yadi.sk``) сначала разрешаются в прямую ссылку
через публичный API Диска.

Затрагивается только справочник (марки/модели); сбор объявлений — нет.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import quote, urlparse

import requests

from common import constants

logger = logging.getLogger("aprice.client.db_update")

SQLITE_MAGIC = b"SQLite format 3\x00"
REQUIRED_TABLES = ("manufacturers", "models")
YANDEX_HOSTS = ("disk.yandex.ru", "disk.yandex.com", "disk.yandex.by",
                "disk.yandex.kz", "yadi.sk")
YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key="
DEFAULT_TIMEOUT = 30
#: Защита от «бесконечной» загрузки: справочник — единицы мегабайт.
MAX_BYTES = 500 * 1024 * 1024
CHUNK = 64 * 1024

PathLike = Union[Path, str]
ProgressCb = Callable[[int, Optional[int]], None]


class DbUpdateError(Exception):
    """Обновление не выполнено; текст пригоден для показа пользователю."""


@dataclass(frozen=True)
class UpdateResult:
    """Итог успешного обновления."""

    path: Path
    backup: Optional[Path]
    size: int
    manufacturers: int


def db_age_days(path: PathLike, now: Optional[float] = None) -> Optional[int]:
    """Возраст файла базы в целых днях по времени изменения; ``None`` — файла нет."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    return max(0, int(((now if now is not None else time.time()) - mtime) // 86400))


def _check_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DbUpdateError("Укажите ссылку на базу (http:// или https://) в настройках.")
    return url


def _resolve(url: str, get: Callable[..., Any], timeout: float) -> str:
    """Публичную ссылку Яндекс.Диска превращает в прямую; остальные — как есть."""
    if (urlparse(url).hostname or "").lower() not in YANDEX_HOSTS:
        return url
    try:
        response = get(YANDEX_API + quote(url, safe=""), timeout=timeout)
        response.raise_for_status()
        href = response.json().get("href")
    except (requests.RequestException, ValueError) as exc:
        raise DbUpdateError(f"Не удалось получить ссылку на скачивание с Яндекс.Диска: {exc}") from exc
    if not isinstance(href, str) or not href:
        raise DbUpdateError("Яндекс.Диск не вернул ссылку на скачивание (файл удалён или закрыт?).")
    return href


def _download(url: str, tmp: Path, get: Callable[..., Any], timeout: float,
              progress: Optional[ProgressCb]) -> int:
    try:
        response = get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        total = response.headers.get("Content-Length")
        total_int = int(total) if total and total.isdigit() else None
        if total_int is not None and total_int > MAX_BYTES:
            raise DbUpdateError("Файл по ссылке слишком большой для базы справочника.")
        done = 0
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                done += len(chunk)
                if done > MAX_BYTES:
                    raise DbUpdateError("Файл по ссылке слишком большой для базы справочника.")
                fh.write(chunk)
                if progress:
                    progress(done, total_int)
        return done
    except requests.RequestException as exc:
        raise DbUpdateError(f"Не удалось скачать базу: {exc}") from exc
    except OSError as exc:
        raise DbUpdateError(f"Не удалось сохранить скачанный файл: {exc}") from exc


def validate_database(path: PathLike) -> int:
    """Проверяет файл базы; возвращает число марок.

    Raises:
        DbUpdateError: не SQLite, ``integrity_check`` не ``ok`` или нет справочника.
    """
    path = Path(path)
    try:
        with path.open("rb") as fh:
            if fh.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                raise DbUpdateError("Скачанный файл не является базой SQLite (проверьте ссылку).")
    except OSError as exc:
        raise DbUpdateError(f"Не удалось прочитать скачанный файл: {exc}") from exc
    conn = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        verdict = conn.execute("PRAGMA integrity_check").fetchall()
        if verdict != [("ok",)]:
            raise DbUpdateError("Проверка целостности (PRAGMA integrity_check) не пройдена: "
                                "скачанная база повреждена.")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise DbUpdateError(f"В скачанной базе нет таблиц справочника: {', '.join(missing)}.")
        count = conn.execute("SELECT COUNT(*) FROM manufacturers").fetchone()[0]
        if count == 0:
            raise DbUpdateError("Скачанная база пуста: в справочнике нет марок.")
        return int(count)
    except sqlite3.Error as exc:
        raise DbUpdateError(f"Скачанный файл не читается как база: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def download_database(
    url: str,
    target: Optional[PathLike] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    progress: Optional[ProgressCb] = None,
    get: Optional[Callable[..., Any]] = None,
) -> UpdateResult:
    """Скачивает, проверяет и подменяет ``aprice.db`` (см. описание модуля).

    Args:
        url: ``db_update_url`` из настроек.
        target: файл рабочей базы (по умолчанию ``constants.DB_FILE``).
        timeout: таймаут сетевых запросов, сек.
        progress: ``progress(скачано_байт, всего_или_None)``.
        get: функция запроса (по умолчанию ``requests.get``, берётся в момент вызова —
            её можно подменить в тестах через ``monkeypatch``).

    Raises:
        DbUpdateError: любая причина отказа; рабочая база не изменена.
    """
    target_path = Path(target) if target is not None else constants.DB_FILE
    url = _check_url(url)
    getter = get if get is not None else requests.get
    tmp = target_path.with_name(target_path.name + ".download")
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        direct = _resolve(url, getter, timeout)
        size = _download(direct, tmp, getter, timeout, progress)
        manufacturers = validate_database(tmp)
        backup: Optional[Path] = None
        if target_path.exists():
            backup = target_path.with_name(target_path.name + ".bak")
            shutil.copy2(target_path, backup)
        try:
            os.replace(tmp, target_path)
        except OSError as exc:
            raise DbUpdateError(
                f"Не удалось заменить базу (файл занят другой программой?): {exc}"
            ) from exc
    except OSError as exc:
        raise DbUpdateError(f"Ошибка работы с файлами: {exc}") from exc
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Временный файл %s не удалён", tmp)
    logger.info("База обновлена: %s (%d байт, марок: %d)", target_path, size, manufacturers)
    return UpdateResult(target_path, backup, size, manufacturers)
