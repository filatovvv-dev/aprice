"""Окно настроек APrice-Client (подзадача 4.4).

Меняет и сохраняет в ``config/settings.json`` (секция ``client``):

* ``max_pages_per_site``, ``max_ads_per_site`` — лимиты сбора (уходят в ``options``
  при отправке задания);
* ``db_update_url`` — зеркало справочника ``aprice.db``;
* ``db_date_url`` — ``date.txt`` рядом с базой (4.5): база скачивается, только если дата
  в нём новее локальной; пусто — проверка пропускается;
* ``db_max_age_days`` — через сколько дней база считается устаревшей (подсказка в окне).

Кнопка «Обновить базу из зеркала» скачивает базу в фоне (:class:`DbUpdateThread`,
GUI не зависает), логика — в :mod:`client.db_update`. Пока идёт загрузка, окно
нельзя закрыть. Позицию окна задаёт ``WindowManager.apply(dialog, "settings_dialog")``
(вызывает контроллер).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client.db_update import (
    DbUpdateError,
    UpdateResult,
    db_age_days,
    download_database,
    fetch_remote_date,
    is_remote_newer,
    local_db_date,
)
from common import constants
from common.config import Config
from common.footer import CopyrightFooter

logger = logging.getLogger("aprice.client.settings")

SECTION = "client"
WINDOW_ID = "settings_dialog"
TITLE = "Настройки"
MAX_PAGES_RANGE = (1, 1000)
MAX_ADS_RANGE = (1, 100_000)
MAX_AGE_RANGE = (1, 3650)
STALE_STYLE = "color: red;"


def url_error(url: str) -> Optional[str]:
    """Текст ошибки для ``db_update_url`` или ``None``. Пустая ссылка допустима."""
    url = url.strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Ссылка на базу должна начинаться с http:// или https://"
    return None


class DbUpdateThread(QThread):
    """Скачивает базу в фоновом потоке; результат — сигналами (в GUI-потоке)."""

    succeeded = pyqtSignal(object)  # UpdateResult
    up_to_date = pyqtSignal()  # date.txt не новее локальной базы — скачивать не нужно (4.5)
    failed = pyqtSignal(str)
    progressed = pyqtSignal(int, int)  # скачано, всего (-1 — неизвестно)

    def __init__(
        self,
        url: str,
        target: Optional[Path] = None,
        parent=None,
        downloader: Callable[..., UpdateResult] = download_database,
        date_url: str = "",
        date_fetcher: Callable[..., object] = fetch_remote_date,
    ) -> None:
        super().__init__(parent)
        self._url, self._target, self._downloader = url, target, downloader
        self._date_url, self._date_fetcher = date_url, date_fetcher

    def run(self) -> None:
        try:
            extra: dict = {}
            if self._date_url:
                # Сначала крошечный date.txt; базу качаем, только если дата строго новее.
                remote = self._date_fetcher(self._date_url)
                local = local_db_date(self._target if self._target is not None else constants.DB_FILE)
                if not is_remote_newer(remote, local):
                    self.up_to_date.emit()
                    return
                extra["mtime"] = remote.timestamp()
            result = self._downloader(
                self._url, self._target,
                progress=lambda done, total: self.progressed.emit(done, -1 if total is None else total),
                **extra,
            )
        except DbUpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 — поток не должен падать молча
            logger.exception("Непредвиденная ошибка обновления базы")
            self.failed.emit(f"Непредвиденная ошибка: {exc}")
        else:
            self.succeeded.emit(result)


class SettingsDialog(QDialog):
    """Диалог настроек; значения читает из :class:`Config` и пишет обратно по «Сохранить»."""

    #: База справочника успешно обновлена (аргумент — :class:`UpdateResult`).
    database_updated = pyqtSignal(object)

    def __init__(
        self,
        config: Config,
        parent: Optional[QWidget] = None,
        db_path: Optional[Path] = None,
        thread_factory: Callable[[str, Path, str], DbUpdateThread] = DbUpdateThread,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._db_path = Path(db_path) if db_path is not None else None
        self._thread_factory = thread_factory
        self._thread: Optional[DbUpdateThread] = None
        self.setWindowTitle(TITLE)

        self.max_pages_spin = QSpinBox()
        self.max_pages_spin.setRange(*MAX_PAGES_RANGE)
        self.max_ads_spin = QSpinBox()
        self.max_ads_spin.setRange(*MAX_ADS_RANGE)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://… (прямая ссылка или публичная ссылка Яндекс.Диска)")
        self.date_url_edit = QLineEdit()
        self.date_url_edit.setPlaceholderText("https://… ссылка на date.txt (пусто — всегда скачивать базу)")
        self.age_spin = QSpinBox()
        self.age_spin.setRange(*MAX_AGE_RANGE)
        self.db_age_label = QLabel("")
        self.update_status_label = QLabel("")
        self.update_button = QPushButton("Обновить базу из зеркала")
        self.save_button = QPushButton("Сохранить")
        self.cancel_button = QPushButton("Отмена")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Сбор данных"))
        layout.addLayout(self._row("Страниц с площадки (максимум):", self.max_pages_spin))
        layout.addLayout(self._row("Объявлений с площадки (максимум):", self.max_ads_spin))
        layout.addWidget(QLabel("База справочников"))
        layout.addLayout(self._row("Ссылка на зеркало:", self.url_edit))
        layout.addLayout(self._row("Ссылка на дату обновления:", self.date_url_edit))
        layout.addLayout(self._row("Считать базу устаревшей через (дней):", self.age_spin))
        layout.addWidget(self.db_age_label)
        layout.addWidget(self.update_button)
        layout.addWidget(self.update_status_label)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.footer_label = CopyrightFooter("Client", self)
        layout.addWidget(self.footer_label)

        self.save_button.clicked.connect(lambda _c=False: self.save())
        self.cancel_button.clicked.connect(lambda _c=False: self.reject())
        self.update_button.clicked.connect(lambda _c=False: self.start_update())
        self.age_spin.valueChanged.connect(lambda _v: self._refresh_age())
        self.load_values()

    @staticmethod
    def _row(caption: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(caption))
        row.addWidget(widget)
        return row

    # ------------------------------------------------------------------
    # Значения <-> Config
    # ------------------------------------------------------------------
    def load_values(self) -> None:
        """Заполняет поля из конфига."""
        get = self._config.get
        self.max_pages_spin.setValue(int(get(SECTION, "max_pages_per_site", 10)))
        self.max_ads_spin.setValue(int(get(SECTION, "max_ads_per_site", 500)))
        self.url_edit.setText(str(get(SECTION, "db_update_url", "") or ""))
        self.date_url_edit.setText(str(get(SECTION, "db_date_url", "") or ""))
        self.age_spin.setValue(int(get(SECTION, "db_max_age_days", 180)))
        self._refresh_age()

    def values(self) -> dict:
        """Значения полей как словарь ключей конфига."""
        return {
            "max_pages_per_site": self.max_pages_spin.value(),
            "max_ads_per_site": self.max_ads_spin.value(),
            "db_update_url": self.url_edit.text().strip(),
            "db_date_url": self.date_url_edit.text().strip(),
            "db_max_age_days": self.age_spin.value(),
        }

    def save(self) -> bool:
        """Проверяет и сохраняет настройки; ``False`` — остались в окне (ошибка показана)."""
        if self.is_updating():
            return False
        values = self.values()
        error = url_error(values["db_update_url"]) or url_error(values["db_date_url"])
        if error:
            QMessageBox.critical(self, TITLE, error)
            return False
        for key, value in values.items():
            self._config.set(SECTION, key, value)
        try:
            self._config.save()
        except OSError as exc:
            logger.error("settings.json не записан: %s", exc)
            QMessageBox.critical(self, TITLE, f"Не удалось сохранить настройки:\n{exc}")
            return False
        self.accept()
        return True

    # ------------------------------------------------------------------
    # Возраст базы
    # ------------------------------------------------------------------
    def _db_file(self) -> Path:
        return self._db_path if self._db_path is not None else constants.DB_FILE

    def _refresh_age(self) -> None:
        age = db_age_days(self._db_file())
        if age is None:
            self.db_age_label.setText("Файл справочника не найден — обновите базу.")
            self.db_age_label.setStyleSheet(STALE_STYLE)
        elif age > self.age_spin.value():
            self.db_age_label.setText(f"Справочник устарел: обновлялся {age} дн. назад.")
            self.db_age_label.setStyleSheet(STALE_STYLE)
        else:
            self.db_age_label.setText(f"Справочник обновлялся {age} дн. назад.")
            self.db_age_label.setStyleSheet("")

    # ------------------------------------------------------------------
    # Обновление базы из зеркала
    # ------------------------------------------------------------------
    def is_updating(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start_update(self) -> None:
        """Запускает фоновое скачивание по ссылке из поля (даже если ещё не сохранена)."""
        if self.is_updating():
            return
        url = self.url_edit.text().strip()
        error = url_error(url)
        if error or not url:
            QMessageBox.critical(self, TITLE, error or "Укажите ссылку на зеркало базы.")
            return
        date_url = self.date_url_edit.text().strip()
        error = url_error(date_url)
        if error:
            QMessageBox.critical(self, TITLE, error)
            return
        self._thread = self._thread_factory(url, self._db_file(), date_url=date_url)
        self._thread.succeeded.connect(self._on_succeeded)
        self._thread.up_to_date.connect(self._on_up_to_date)
        self._thread.failed.connect(self._on_failed)
        self._thread.progressed.connect(self._on_progress)
        self.update_button.setEnabled(False)
        self.update_status_label.setText("Проверка даты обновления…" if date_url else "Загрузка…")
        self._thread.start()

    def _on_progress(self, done: int, total: int) -> None:
        mb = done / 1_048_576
        self.update_status_label.setText(
            f"Загрузка… {mb:.1f} из {total / 1_048_576:.1f} МБ" if total > 0 else f"Загрузка… {mb:.1f} МБ"
        )

    def _on_up_to_date(self) -> None:
        self.update_button.setEnabled(True)
        self.update_status_label.setText("База актуальна.")
        QMessageBox.information(
            self, "Обновления не требуются",
            "У вас уже установлена самая свежая версия базы.",
        )

    def _on_succeeded(self, result: UpdateResult) -> None:
        self.update_button.setEnabled(True)
        self.update_status_label.setText(f"База обновлена, марок: {result.manufacturers}.")
        self._refresh_age()
        self.database_updated.emit(result)
        backup = f"\nРезервная копия: {result.backup.name}" if result.backup else ""
        QMessageBox.information(self, TITLE, f"База справочников обновлена (марок: {result.manufacturers}).{backup}")

    def _on_failed(self, message: str) -> None:
        self.update_button.setEnabled(True)
        self.update_status_label.setText("Обновление не выполнено.")
        QMessageBox.critical(self, TITLE, message)

    def done(self, result: int) -> None:  # noqa: D401 — переопределение Qt
        """Во время загрузки окно закрыть нельзя (иначе поток уничтожится на ходу)."""
        if self.is_updating():
            self.update_status_label.setText("Дождитесь окончания загрузки…")
            return
        super().done(result)
