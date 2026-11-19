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

Манифест обновления (5.5): ``date.txt`` может содержать и вторую строку — актуальную
ссылку на ``aprice.db`` (Яндекс.Диск меняет ссылку при замене файла); без неё берётся
``db_update_url``.

Умное обновление (4.5): рядом с базой на зеркале лежит ``date.txt`` с датой
публикации (ISO 8601, например ``2026-11-17T12:00:00``). :func:`check_for_updates`
сначала скачивает этот крошечный файл и сравнивает дату с локальной базой
(``metadata.last_update``, иначе время изменения файла); базу качаем, только если
удалённая дата **строго новее**. После скачивания время изменения файла
выставляется равным дате публикации, поэтому сравнение остаётся корректным и для
базы, просто скопированной с диска. Дата без часового пояса считается местным временем.

Затрагивается только справочник (марки/модели); сбор объявлений — нет.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
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


MAX_DATE_BYTES = 4096


#: Форматы даты в ``date.txt`` (5.5): ISO 8601 и русский ``DD.MM.YYYY`` (с временем и без).
DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",  # ISO 8601
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",  # русский, с временем
    "%d.%m.%Y",           # русский, без времени
)


def parse_date(date_str: str) -> Optional[datetime]:
    """Разбирает дату из ``date.txt`` (первая непустая строка; BOM и пробелы допускаются).

    Понимает ``DD.MM.YYYY``, ``DD.MM.YYYY HH:MM:SS`` и ISO 8601 (в том числе со
    смещением пояса, ``Z`` и долями секунды — их разбирает ``fromisoformat``).

    Returns:
        datetime: дата; ``None`` — не распознана ни в одном формате (пишется
        предупреждение в лог; вызывающий считает, что обновление не требуется).
    """
    lines = [ln.strip() for ln in (date_str or "").lstrip("\ufeff").splitlines() if ln.strip()]
    if not lines:
        logger.warning("Файл с датой обновления пуст")
        return None
    text = lines[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)  # ISO со смещением/«Z»/долями секунды
    except ValueError:
        logger.warning("Дата обновления не распознана: %r", text[:60])
        return None


@dataclass(frozen=True)
class Manifest:
    """Манифест обновления (``date.txt``): дата публикации и актуальная ссылка на базу.

    Формат (5.5)::

        17.11.2026
        https://disk.yandex.ru/d/новая_ссылка_на_базу

    Вторая строка необязательна (старый формат из одной даты работает как раньше).
    """

    date: Optional[datetime]  # None — первая строка не распознана как дата
    db_url: Optional[str]  # None — второй строки нет/пуста/это не ссылка


def parse_manifest(text: str) -> Manifest:
    """Разбирает текст ``date.txt``: 1-я непустая строка — дата, 2-я — ссылка на базу.

    Пустые строки пропускаются, BOM и пробелы допускаются. Вторая строка, которая не
    является ссылкой ``http(s)``, игнорируется с предупреждением (сработает запасная
    ``db_update_url`` из настроек). Остальные строки не используются.
    """
    lines = [ln.strip() for ln in (text or "").lstrip("\ufeff").splitlines() if ln.strip()]
    date = parse_date(lines[0]) if lines else parse_date("")
    db_url: Optional[str] = None
    if len(lines) > 1:
        candidate = lines[1]
        parsed = urlparse(candidate)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            db_url = candidate
        else:
            logger.warning("Вторая строка date.txt не ссылка, игнорирую: %r", candidate[:80])
    return Manifest(date=date, db_url=db_url)


def fetch_manifest(
    remote_date_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    get: Optional[Callable[..., Any]] = None,
) -> Manifest:
    """Скачивает ``date.txt`` (публичную ссылку Яндекс.Диска разрешает через API) и разбирает его.

    Raises:
        DbUpdateError: ссылка некорректна, сеть недоступна или получена слишком большая
            страница (HTML вместо короткого текста).
    """
    url = _check_url(remote_date_url)
    getter = get if get is not None else requests.get
    direct = _resolve(url, getter, timeout)
    try:
        response = getter(direct, timeout=timeout)
        response.raise_for_status()
        text = response.text
    except requests.RequestException as exc:
        raise DbUpdateError(f"Не удалось получить дату обновления базы: {exc}") from exc
    if len(text) > MAX_DATE_BYTES:
        raise DbUpdateError("Файл с датой обновления слишком большой (получена не та страница?).")
    return parse_manifest(text)


def fetch_remote_date(
    remote_date_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    get: Optional[Callable[..., Any]] = None,
) -> Optional[datetime]:
    """Дата из ``date.txt`` (первая строка манифеста); ``None`` — не распознана."""
    return fetch_manifest(remote_date_url, timeout=timeout, get=get).date


def local_db_date(local_db_path: PathLike) -> Optional[datetime]:
    """Дата локальной базы: ``metadata.last_update`` (ISO), иначе время изменения файла.

    Returns:
        ``None`` — файла нет.
    """
    path = Path(local_db_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute("SELECT value FROM metadata WHERE key = 'last_update'").fetchone()
        parsed = parse_date(str(row[0])) if row and row[0] else None
        if parsed is not None:
            return parsed
    except sqlite3.Error:
        pass  # нет таблицы/ключа — берём mtime
    finally:
        if conn is not None:
            conn.close()
    return datetime.fromtimestamp(mtime)


def is_remote_newer(remote: datetime, local: Optional[datetime]) -> bool:
    """``True``, если ``remote`` строго новее ``local`` (нет локальной базы — тоже ``True``)."""
    if local is None:
        return True
    return remote.timestamp() > local.timestamp()


@dataclass(frozen=True)
class UpdateCheck:
    """Итог проверки манифеста."""

    required: bool  # нужно ли скачивать базу
    db_url: Optional[str]  # ссылка из манифеста (None — брать db_update_url); None и если не нужно
    date: Optional[datetime]  # дата публикации (для времени изменения скачанного файла)


def check_manifest(
    remote_date_url: str,
    local_db_path: PathLike,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    get: Optional[Callable[..., Any]] = None,
) -> UpdateCheck:
    """Скачивает манифест и решает, нужно ли обновление (удалённая дата строго новее).

    Дата не распознана → обновление не требуется (ссылка тоже не возвращается).

    Raises:
        DbUpdateError: манифест получить не удалось (сеть, ссылка).
    """
    manifest = fetch_manifest(remote_date_url, timeout=timeout, get=get)
    if manifest.date is None or not is_remote_newer(manifest.date, local_db_date(local_db_path)):
        return UpdateCheck(False, None, manifest.date)
    return UpdateCheck(True, manifest.db_url, manifest.date)


def check_for_updates(
    remote_date_url: str,
    local_db_path: PathLike,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    get: Optional[Callable[..., Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Нужно ли скачивать базу и по какой ссылке.

    Args:
        remote_date_url: ``db_date_url`` (ссылка на ``date.txt`` — манифест обновления).
        local_db_path: файл рабочей базы.

    Returns:
        tuple: ``(требуется_обновление, url_базы)``. ``url_базы`` — вторая строка
        манифеста; ``None``, если обновление не нужно (дата равна/старше/не распознана)
        или ссылки в манифесте нет (тогда берётся ``db_update_url`` из настроек).

    Raises:
        DbUpdateError: манифест получить не удалось (сеть, ссылка).
    """
    result = check_manifest(remote_date_url, local_db_path, timeout=timeout, get=get)
    return result.required, result.db_url


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
    mtime: Optional[float] = None,
) -> UpdateResult:
    """Скачивает, проверяет и подменяет ``aprice.db`` (см. описание модуля).

    Args:
        url: ``db_update_url`` из настроек.
        target: файл рабочей базы (по умолчанию ``constants.DB_FILE``).
        timeout: таймаут сетевых запросов, сек.
        progress: ``progress(скачано_байт, всего_или_None)``.
        get: функция запроса (по умолчанию ``requests.get``, берётся в момент вызова —
            её можно подменить в тестах через ``monkeypatch``).
        mtime: POSIX-время, которое получит файл после подмены (дата публикации
            из ``date.txt``, 4.5); ``None`` — оставить время скачивания.

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
    if mtime is not None:
        try:
            os.utime(target_path, (mtime, mtime))
        except OSError as exc:  # не критично: база уже заменена
            logger.warning("Не удалось выставить время изменения базы: %s", exc)
    logger.info("База обновлена: %s (%d байт, марок: %d)", target_path, size, manufacturers)
    return UpdateResult(target_path, backup, size, manufacturers)
