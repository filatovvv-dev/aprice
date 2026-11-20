"""Проверка и установка Chromium для Playwright (этап 5.1).

Chromium **не вшивается** в сборку (это сотни мегабайт). При первом запуске
Server проверяет ``pw.chromium.executable_path`` и, если браузера нет, запускает
``playwright install chromium`` — в фоновом потоке, с ``QProgressDialog``.

Логика (проверка, команда, установка) не зависит от Qt и тестируется на моках;
Qt-часть (:class:`QtInstallUi`) импортируется лениво.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Protocol

logger = logging.getLogger("aprice.server.browser_check")

INSTALL_FAILED_MESSAGE = (
    "Не удалось установить браузер. Проверьте соединение или установите вручную"
)
NOT_FOUND_MESSAGE = (
    "Chromium не найден по пути {path}. "
    "Установите его или положите в папку browser/ рядом с программой"
)
INSTALL_TITLE = "АвтоПрайс — установка браузера"
INSTALL_PROGRESS_TEXT = (
    "Установка Chromium...\n"
    "Первый запуск: скачивается браузер (около 150 МБ). Это происходит один раз."
)
INSTALL_TIMEOUT_SEC = 1800  # 30 минут на медленном канале
INSTALL_INDETERMINATE_TEXT = "Установка Chromium..."  # если проценты разобрать не удалось

# --- Ускорение загрузки Chromium (5.6) --------------------------------------------
#: Зеркало Playwright по умолчанию: быстрее официального CDN во многих сетях.
DEFAULT_MIRROR_URL = "https://npmmirror.com/mirrors/playwright"
#: Переменная APrice: пусто/не задана — зеркало по умолчанию; ``off``/``0``/``false``/
#: ``official``/``cdn`` — только официальный CDN; иной текст — свой адрес зеркала.
MIRROR_ENV = "APRICE_CHROMIUM_MIRROR"
MIRROR_OFF_VALUES = frozenset({"off", "0", "false", "no", "official", "cdn", "none"})
#: Собственные переменные Playwright: если пользователь задал их сам, мы их не трогаем.
PLAYWRIGHT_HOST_VARS = ("PLAYWRIGHT_DOWNLOAD_HOST", "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST")


def chromium_executable_path() -> str:
    """Путь, по которому Playwright ожидает Chromium (``pw.chromium.executable_path``).

    Учитывает ``PLAYWRIGHT_BROWSERS_PATH`` (``browser/`` рядом с ``.exe``, 5.2).
    Если Playwright недоступен — возвращает каталог ``browser/`` как подсказку.

    Returns:
        str: путь к ``chrome.exe`` (файл может не существовать) либо каталог ``browser``.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return str(pw.chromium.executable_path or "")
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось определить путь к Chromium")
        return ""


def chromium_installed() -> bool:
    """Проверяет, что исполняемый файл Chromium из Playwright существует.

    Returns:
        bool: ``True`` — браузер установлен; ``False`` — его нет или Playwright
        недоступен/упал при проверке.
    """
    executable = chromium_executable_path()
    return bool(executable) and Path(executable).exists()


def not_found_message() -> str:
    """Сообщение «Chromium не найден по пути …» (с учётом ``browser/`` рядом с ``.exe``)."""
    path = chromium_executable_path() or os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", "browser"
    )
    return NOT_FOUND_MESSAGE.format(path=path)


def _pin_browsers_path(env: dict[str, str]) -> None:
    """Гарантирует, что установка пойдёт в ``PLAYWRIGHT_BROWSERS_PATH`` (browser/ рядом с .exe)."""
    value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if value:
        env["PLAYWRIGHT_BROWSERS_PATH"] = value


def mirror_url(environ: Optional[dict] = None) -> Optional[str]:
    """Адрес зеркала для загрузки Chromium или ``None`` (качаем с официального CDN).

    ``None``, если зеркало выключено (:data:`MIRROR_ENV`) или пользователь сам задал
    ``PLAYWRIGHT_DOWNLOAD_HOST`` / ``PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`` — его выбор приоритетнее.
    """
    env = os.environ if environ is None else environ
    if any(str(env.get(name, "")).strip() for name in PLAYWRIGHT_HOST_VARS):
        return None
    value = str(env.get(MIRROR_ENV, "")).strip()
    if value.lower() in MIRROR_OFF_VALUES:
        return None
    return value or DEFAULT_MIRROR_URL


def build_install_command(mirror: Optional[str] = None) -> tuple[list[str], dict[str, str]]:
    """Формирует команду установки Chromium.

    В собранном ``.exe`` консольной команды ``playwright`` в ``PATH`` нет, поэтому
    используется встроенный драйвер Playwright (``node`` + ``cli.js``), который
    PyInstaller кладёт в сборку через ``collect_all('playwright')``. Если драйвер
    найти не удалось, применяется прямая команда ``playwright install chromium``.

    Args:
        mirror: адрес зеркала → ``PLAYWRIGHT_DOWNLOAD_HOST`` в окружении процесса
            (``None`` — официальный CDN).

    Returns:
        tuple: ``(argv, env)`` для :func:`subprocess.run`.
    """
    argv, env = _base_install_command()
    if mirror:
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = mirror
    return argv, env


def _base_install_command() -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        driver = compute_driver_executable()
        node, cli = (driver if isinstance(driver, tuple) else (str(driver), None))
        argv = [str(node)] + ([str(cli)] if cli else []) + ["install", "chromium"]
        env = dict(get_driver_env())
        _pin_browsers_path(env)
        return argv, env
    except Exception:  # noqa: BLE001 - запасной вариант из ТЗ
        logger.warning("Драйвер Playwright не найден, использую команду 'playwright'")
        _pin_browsers_path(env)
        return ["playwright", "install", "chromium"], env


# --- Прогресс установки (5.6) ------------------------------------------------------
_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*%")
_TOTAL_RE = re.compile(r"of\s+([\d.]+)\s*(KiB|MiB|GiB|KB|MB|GB)", re.IGNORECASE)
_SPEED_RE = re.compile(r"([\d.]+)\s*(KiB|MiB|GiB|KB|MB|GB)\s*/\s*s", re.IGNORECASE)
_UNIT_MB = {"kib": 1 / 1024, "kb": 1 / 1024, "mib": 1.0, "mb": 1.0, "gib": 1024.0, "gb": 1024.0}


def _to_mb(value: str, unit: str) -> float:
    return float(value) * _UNIT_MB[unit.lower()]


def format_speed(mb_per_s: float) -> str:
    """``1.2 MB/s`` (либо ``850 KB/s`` для малых скоростей)."""
    if mb_per_s >= 1:
        return f"{mb_per_s:.1f} MB/s"
    return f"{mb_per_s * 1024:.0f} KB/s"


class ProgressParser:
    """Разбирает вывод ``playwright install``: проценты и скорость.

    Понимает строки вида ``Downloading Chromium ... 45% 1.2 MB/s`` и
    ``|■■■■    |  45% of 150.2 MiB``. Если скорость в строке не указана, она считается
    по доле от общего размера и прошедшему времени. Строки без процентов игнорируются.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Запоминает момент начала загрузки (для расчёта скорости)."""
        self._clock = clock
        self._start: Optional[float] = None
        self._last_percent = -1

    def feed(self, text: str) -> Optional[tuple[int, Optional[str]]]:
        """Обрабатывает фрагмент вывода.

        Returns:
            ``(процент, скорость_или_None)`` либо ``None``, если процентов нет или
            значение не изменилось.
        """
        found = _PERCENT_RE.findall(text)
        if not found:
            return None
        percent = int(found[-1])
        if not 0 <= percent <= 100 or percent == self._last_percent:
            return None
        now = self._clock()
        if self._start is None:
            self._start = now
        self._last_percent = percent
        speed: Optional[str] = None
        match = _SPEED_RE.search(text)
        if match:
            speed = format_speed(_to_mb(match.group(1), match.group(2)))
        else:
            total = _TOTAL_RE.search(text)
            elapsed = now - self._start
            if total and elapsed > 0 and percent > 0:
                speed = format_speed(_to_mb(total.group(1), total.group(2)) * percent / 100 / elapsed)
        return percent, speed


ProgressCallback = Callable[[Optional[int], Optional[str]], None]


def run_streaming(
    argv: list[str],
    *,
    on_progress: Optional[ProgressCallback] = None,
    timeout: float = INSTALL_TIMEOUT_SEC,
    popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
    **kwargs,
) -> "subprocess.CompletedProcess":
    """Запускает процесс через ``Popen`` и разбирает его вывод на проценты/скорость.

    stderr объединяется со stdout (Playwright пишет прогресс то в один, то в другой поток;
    строки прогресса разделяются ``\\r``). ``on_progress(percent, speed)`` вызывается из
    потока чтения при каждом изменении процента.

    Returns:
        :class:`subprocess.CompletedProcess` с объединённым выводом в ``stdout``.

    Raises:
        subprocess.TimeoutExpired: время вышло (процесс убит).
    """
    kwargs.pop("capture_output", None)
    kwargs.pop("text", None)
    proc = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    parser = ProgressParser()
    chunks: list[str] = []

    def reader() -> None:
        stream = proc.stdout
        if stream is None:
            return
        buffer = ""
        while True:
            data = stream.read1(1024) if hasattr(stream, "read1") else stream.read(1024)
            if not data:
                break
            buffer += data.decode("utf-8", errors="replace")
            *parts, buffer = re.split(r"[\r\n]", buffer)
            for part in parts:
                if not part.strip():
                    continue
                chunks.append(part)
                if on_progress is not None:
                    try:
                        update = parser.feed(part)
                        if update is not None:
                            on_progress(update[0], update[1])
                    except Exception:  # noqa: BLE001 - прогресс не должен ронять установку
                        logger.debug("Не удалось разобрать строку прогресса", exc_info=True)
        if buffer.strip():
            chunks.append(buffer)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        thread.join(5)
        raise
    thread.join(10)
    return subprocess.CompletedProcess(argv, code, stdout="\n".join(chunks), stderr="")


def _run_install(
    runner: Callable[..., "subprocess.CompletedProcess"],
    mirror: Optional[str],
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[bool, str]:
    """Один запуск ``playwright install chromium`` (с зеркалом или без)."""
    argv, env = build_install_command(mirror)
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "env": env,
        "timeout": INSTALL_TIMEOUT_SEC,
    }
    if sys.platform == "win32":  # без мигающего консольного окна
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if on_progress is not None:
        kwargs["on_progress"] = on_progress
    source = mirror or "официальный CDN"
    try:
        result = runner(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Запуск установки Chromium (%s) не удался: %s", source, exc)
        return False, str(exc)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        logger.error("playwright install (%s) завершился с кодом %s: %s", source, result.returncode, output)
        return False, output
    logger.info("Chromium установлен (источник: %s)", source)
    return True, output


def install_chromium(
    runner: Callable[..., "subprocess.CompletedProcess"] = run_streaming,
    environ: Optional[dict] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[bool, str]:
    """Устанавливает Chromium (``playwright install chromium``).

    Сначала пробует зеркало (:func:`mirror_url`); если оно недоступно или установка
    не удалась — автоматически повторяет с официального CDN (откат, 5.6).

    Args:
        runner: функция запуска процесса (подменяется в тестах).
        environ: окружение для выбора зеркала (по умолчанию ``os.environ``).
        on_progress: ``(процент, скорость)`` — обновление индикатора; при откате на
            CDN процент начинается заново.

    Returns:
        tuple: ``(успех, подробности_для_лога)``.
    """
    mirror = mirror_url(environ)
    ok, details = _run_install(runner, mirror, on_progress)
    if ok or mirror is None:
        return ok, details
    logger.warning("Зеркало %s не сработало — повторяю с официального CDN", mirror)
    if on_progress is not None:
        on_progress(None, None)  # сброс: снова «Установка Chromium...»
    ok_cdn, details_cdn = _run_install(runner, None, on_progress)
    return ok_cdn, details_cdn if ok_cdn else f"{details}\n{details_cdn}".strip()


class InstallUi(Protocol):
    """Интерфейс показа процесса установки (Qt-реализация — :class:`QtInstallUi`)."""

    def run(self, work: Callable[[], tuple[bool, str]]) -> Optional[tuple[bool, str]]:
        """Выполняет ``work`` с индикатором; ``None`` — пользователь отменил."""

    def show_error(self, message: str) -> None:
        """Показывает сообщение об ошибке."""


def progress_text(percent: Optional[int], speed: Optional[str]) -> str:
    """Текст под индикатором: ``Скачивание Chromium: 45% · 1.2 MB/s`` либо «Установка Chromium...»."""
    if percent is None:
        return INSTALL_INDETERMINATE_TEXT
    return f"Скачивание Chromium: {percent}%" + (f" · {speed}" if speed else "")


class QtInstallUi:
    """Диалог установки: ``QProgressDialog`` (``QProgressBar`` + ``QLabel``), работа в ``QThread``.

    Пока проценты неизвестны — индикатор «занят» (indeterminate) с текстом
    «Установка Chromium...»; как только из вывода разобран процент — полоса
    переключается на 0–100 и показывает процент и скорость.
    """

    def __init__(self, parent=None) -> None:
        """Сохраняет родительский виджет (может быть ``None``)."""
        self._parent = parent
        self._emit_progress: Optional[Callable[[int, str], None]] = None

    def report_progress(self, percent: Optional[int], speed: Optional[str]) -> None:
        """Потокобезопасно передаёт прогресс в окно (вызывается из рабочего потока)."""
        emit = self._emit_progress
        if emit is not None:
            emit(-1 if percent is None else int(percent), speed or "")

    def run(self, work: Callable[[], tuple[bool, str]]) -> Optional[tuple[bool, str]]:
        """Показывает ``QProgressDialog`` и ждёт завершения ``work`` в потоке."""
        from PyQt6.QtCore import QEventLoop, QThread, Qt, pyqtSignal
        from PyQt6.QtWidgets import QProgressDialog

        class _Worker(QThread):
            done = pyqtSignal(bool, str)
            progress = pyqtSignal(int, str)

            def run(self) -> None:  # noqa: D102
                try:
                    ok, details = work()
                except Exception as exc:  # noqa: BLE001
                    ok, details = False, str(exc)
                self.done.emit(ok, details)

        dialog = QProgressDialog(INSTALL_PROGRESS_TEXT, "Отмена", 0, 0, self._parent)
        dialog.setWindowTitle(INSTALL_TITLE)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)  # показать сразу, не через 4 с
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setCancelButton(None)  # прерывать скачивание на полпути нельзя
        dialog.setValue(0)

        loop = QEventLoop()
        outcome: list[tuple[bool, str]] = []
        worker = _Worker()

        def _progress(percent: int, speed: str) -> None:
            if percent < 0:  # разобрать не удалось / сброс: indeterminate
                dialog.setRange(0, 0)
                dialog.setLabelText(INSTALL_INDETERMINATE_TEXT)
                return
            if dialog.maximum() != 100:
                dialog.setRange(0, 100)
            dialog.setValue(percent)
            dialog.setLabelText(progress_text(percent, speed or None))

        def _finished(ok: bool, details: str) -> None:
            outcome.append((ok, details))
            loop.quit()

        worker.progress.connect(_progress)  # сигнал из рабочего потока → слот в GUI-потоке
        worker.done.connect(_finished)
        self._emit_progress = worker.progress.emit
        dialog.show()
        worker.start()
        loop.exec()
        worker.wait()
        self._emit_progress = None
        dialog.close()
        return outcome[0] if outcome else None

    def show_error(self, message: str) -> None:
        """Показывает ``QMessageBox.warning`` с текстом ошибки."""
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.warning(self._parent, INSTALL_TITLE, message)


def ensure_chromium(
    ui: Optional[InstallUi] = None,
    *,
    is_installed: Callable[[], bool] = chromium_installed,
    installer: Callable[[], tuple[bool, str]] = install_chromium,
) -> bool:
    """Гарантирует наличие Chromium: проверяет и при необходимости устанавливает.

    Args:
        ui: индикатор установки (по умолчанию Qt-диалог).
        is_installed: проверка наличия браузера (подменяется в тестах).
        installer: установка браузера (подменяется в тестах).

    Returns:
        bool: ``True``, если Chromium доступен после вызова. При неудаче
        пользователю показано :data:`INSTALL_FAILED_MESSAGE`.
    """
    if is_installed():
        return True
    logger.info("Chromium не найден — запускаю установку")
    ui = ui if ui is not None else QtInstallUi()
    report = getattr(ui, "report_progress", None)
    # Индикатор умеет показывать проценты — передаём установщику callback прогресса.
    work = (lambda: installer(on_progress=report)) if callable(report) else installer
    result = ui.run(work)
    if result is not None and result[0] and is_installed():
        return True
    hint = not_found_message()
    logger.error("Chromium не установлен: %s. %s", result[1] if result else "отменено", hint)
    ui.show_error(f"{INSTALL_FAILED_MESSAGE}\n\n{hint}")
    return False
