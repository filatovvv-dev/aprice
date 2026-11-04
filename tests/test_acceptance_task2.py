"""Приёмо-сдаточные проверки задачи №2 (см. промпт, раздел 4).

Запуск из корня проекта: ``pytest tests/ -q``. Тесты не требуют сети;
GUI-проверки выполняются на offscreen-платформе Qt.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from builder import migrations
from builder.database import Database
from builder.parser import CaptchaDetectedError, CatalogParser
from common import constants
from common.config import Config
from common.window_manager import WindowManager


# ---------------------------------------------------------------------------
# Фикстуры и утилиты
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_config(tmp_path: Path) -> Config:
    cfg = Config(path=tmp_path / "settings.json")
    cfg.load()
    return cfg


@pytest.fixture()
def parser_factory(tmp_path: Path):
    """Создаёт CatalogParser поверх изолированной БД и кэша.

    Каталог кэша перенаправляется в tmp-директорию, чтобы тесты не
    использовали общий ``data/cache/`` (иначе закэшированный реальный
    ответ скрывал бы сетевые моки).
    """

    def _make(config: Config | None = None, **kwargs) -> CatalogParser:
        db = Database(db_path=tmp_path / "test.db")
        db.init_schema()
        cfg = config or Config(path=tmp_path / "settings.json").load()
        cache_dir = tmp_path / f"cache_{len(list(tmp_path.glob('cache_*')))}"
        return CatalogParser(db=db, config=cfg, cache_dir=cache_dir, **kwargs)

    return _make


class FakeResponse:
    """Имитация ответа urllib.request.urlopen для тестов детекта CAPTCHA."""

    def __init__(self, body: str, content_type: str, url: str, status: int = 200):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body.encode("utf-8")
        self._url = url

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, response=None, http_error=None):
    def fake_urlopen(request, timeout=None):
        if http_error is not None:
            raise http_error
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


# ---------------------------------------------------------------------------
# Критерий 1.2: детект CAPTCHA без ложных срабатываний
# ---------------------------------------------------------------------------
def test_captcha_json_with_captcha_word_parses_ok(parser_factory, monkeypatch):
    """(а) JSON с полем description="captcha" парсится без ошибки."""
    parser = parser_factory()
    parser._delay_range = (0, 0)
    body = json.dumps({"brands": [{"name": "Lada", "description": "captcha"}]})
    _patch_urlopen(
        monkeypatch,
        FakeResponse(body, "application/json; charset=utf-8", "https://api.cars-base.ru/full"),
    )
    data = parser._http_json_cached("https://api.cars-base.ru/full")
    assert data["brands"][0]["description"] == "captcha"


def test_captcha_html_showcaptcha_detected(parser_factory, monkeypatch):
    """(б) HTML-страница showcaptcha → CaptchaDetectedError."""
    parser = parser_factory()
    parser._delay_range = (0, 0)
    html = "<html><head><title>showcaptcha</title></head><body>вы человек?</body></html>"
    _patch_urlopen(monkeypatch, FakeResponse(html, "text/html", "https://auto.ru/cars/all/"))
    with pytest.raises(CaptchaDetectedError):
        parser._http_json_cached("https://auto.ru/cars/all/")


def test_captcha_redirect_url_detected(parser_factory, monkeypatch):
    """Редирект на showcaptcha-URL (без слова в теле) тоже детектируется."""
    parser = parser_factory()
    parser._delay_range = (0, 0)
    _patch_urlopen(
        monkeypatch,
        FakeResponse("<html>ok</html>", "text/html", "https://auto.ru/showcaptcha?cc=1"),
    )
    with pytest.raises(CaptchaDetectedError):
        parser._http_json_cached("https://auto.ru/cars/all/")


def test_captcha_http_403_detected(parser_factory, monkeypatch):
    """(в) HTTP 403 → CaptchaDetectedError."""
    parser = parser_factory()
    parser._delay_range = (0, 0)
    err = urllib.error.HTTPError("https://api.cars-base.ru/full", 403, "Forbidden", {}, None)
    _patch_urlopen(monkeypatch, http_error=err)
    with pytest.raises(CaptchaDetectedError):
        parser._http_json_cached("https://api.cars-base.ru/full")


def test_captcha_http_429_detected(parser_factory, monkeypatch):
    """HTTP 429 → CaptchaDetectedError."""
    parser = parser_factory()
    parser._delay_range = (0, 0)
    err = urllib.error.HTTPError("https://api.cars-base.ru/full", 429, "Too Many", {}, None)
    _patch_urlopen(monkeypatch, http_error=err)
    with pytest.raises(CaptchaDetectedError):
        parser._http_json_cached("https://api.cars-base.ru/full")


def test_non_captcha_bad_json_is_runtime_error(parser_factory, monkeypatch):
    """Не-JSON без признаков CAPTCHA → RuntimeError о битом JSON, не Captcha."""
    parser = parser_factory()
    parser._delay_range = (0, 0)
    _patch_urlopen(monkeypatch, FakeResponse("<html>maintenance</html>", "text/html", "https://x/y"))
    with pytest.raises(RuntimeError, match="Некорректный JSON"):
        parser._http_json_cached("https://x/y")


# ---------------------------------------------------------------------------
# Критерий 1.1: дружелюбное сообщение при отсутствии браузера Playwright
# ---------------------------------------------------------------------------
def test_missing_browser_friendly_message(parser_factory, monkeypatch):
    """Chromium не установлен → RuntimeError с 'playwright install chromium'."""
    pytest.importorskip("playwright.sync_api")
    parser = parser_factory()

    class FakeChromium:
        executable_path = "/nonexistent/path/chrome-linux/chrome"

        def launch(self, **kwargs):
            raise AssertionError("launch() не должен вызываться без установленного браузера")

    class FakePW:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: FakePW())
    with pytest.raises(RuntimeError, match="playwright install chromium"):
        parser._fetch_via_autoru_html()


# ---------------------------------------------------------------------------
# Критерии 1.3 + 6: схема v2, миграция, UPSERT конфигураций
# ---------------------------------------------------------------------------
def _create_v1_db(path: Path) -> sqlite3.Connection:
    """Создаёт базу по схеме v1 (без UNIQUE) с данными и дубликатом бизнес-ключа."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, source_id TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE models (id INTEGER PRIMARY KEY AUTOINCREMENT, manufacturer_id INTEGER NOT NULL, name TEXT NOT NULL, source_id TEXT, updated_at TEXT NOT NULL, UNIQUE (manufacturer_id, name));
        CREATE TABLE generations (id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL, name TEXT NOT NULL, year_from INTEGER, year_to INTEGER, source_id TEXT, updated_at TEXT NOT NULL, UNIQUE (model_id, name));
        CREATE TABLE configurations (id INTEGER PRIMARY KEY AUTOINCREMENT, generation_id INTEGER NOT NULL, transmission TEXT, fuel_type TEXT, engine_volume_from REAL, engine_volume_to REAL, power_from INTEGER, power_to INTEGER, updated_at TEXT NOT NULL);
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO metadata VALUES ('schema_version', '1');
        INSERT INTO manufacturers VALUES (1, 'Lada', null, 't');
        INSERT INTO models VALUES (1, 1, 'Vesta', null, 't');
        INSERT INTO generations VALUES (1, 1, 'I', 2015, 2022, null, 't');
        INSERT INTO configurations VALUES (1, 1, 'МКПП', 'Бензин', 1.6, 1.6, 106, 106, 't');
        INSERT INTO configurations VALUES (2, 1, 'АКПП', 'Бензин', 1.6, 1.8, 110, 122, 't');
        INSERT INTO configurations VALUES (3, 1, 'МКПП', 'Бензин', 1.8, 1.8, 145, 145, 't');
        """
    )
    conn.commit()
    return conn


def test_migration_v1_to_v2(tmp_path: Path):
    """Миграция: UNIQUE появился, данные перенесены, дубликаты схлопнуты, версия=2."""
    db_file = tmp_path / "aprice.db"
    conn = _create_v1_db(db_file)
    assert migrations.get_schema_version(conn) == 1
    assert not migrations.has_configurations_unique_index(conn)

    performed = migrations.migrate_to_v2(conn, db_path=db_file)
    assert performed is True
    assert migrations.has_configurations_unique_index(conn)
    assert migrations.get_schema_version(conn) == 2
    rows = conn.execute("SELECT COUNT(*) FROM configurations").fetchone()[0]
    # Было 3 строки, две с одинаковым бизнес-ключом (1, МКПП, Бензин):
    # INSERT OR IGNORE оставляет первую (id=1) — итого 2.
    assert rows == 2
    kept = conn.execute(
        "SELECT engine_volume_from FROM configurations WHERE transmission='МКПП'"
    ).fetchone()[0]
    assert kept == 1.6
    assert Path(str(db_file) + ".bak-v1").exists()  # обязательный бэкап
    conn.close()


def test_migration_collapses_extra_duplicates(tmp_path: Path):
    """Три записи с одним бизнес-ключом после миграции = одна."""
    db_file = tmp_path / "dup.db"
    conn = _create_v1_db(db_file)
    conn.execute(
        "INSERT INTO configurations (generation_id, transmission, fuel_type, "
        "engine_volume_from, engine_volume_to, power_from, power_to, updated_at) "
        "VALUES (1, 'МКПП', 'Бензин', 2.0, 2.0, 150, 150, 't')"
    )
    conn.commit()
    migrations.migrate_to_v2(conn, db_path=db_file)
    rows = conn.execute(
        "SELECT COUNT(*) FROM configurations "
        "WHERE generation_id=1 AND transmission='МКПП' AND fuel_type='Бензин'"
    ).fetchone()[0]
    assert rows == 1
    conn.close()


def test_migration_idempotent(tmp_path: Path):
    """Повторный вызов migrate_to_v2 ничего не делает и не падает."""
    db_file = tmp_path / "idem.db"
    conn = _create_v1_db(db_file)
    assert migrations.migrate_to_v2(conn, db_path=db_file) is True
    assert migrations.migrate_to_v2(conn, db_path=db_file) is False
    conn.close()


def test_upsert_configuration_on_conflict(tmp_path: Path):
    """ON CONFLICT DO UPDATE: повторный upsert не создаёт дубликат и обновляет поля."""
    db = Database(db_path=tmp_path / "up.db")
    db.init_schema()
    man = db.upsert_manufacturer("Lada")
    mod = db.upsert_model(man, "Vesta")
    gen = db.upsert_generation(mod, "I", 2015, 2022)
    db.upsert_configuration(gen, "МКПП", "Бензин", 1.6, 1.6, 106, 106)
    db.upsert_configuration(gen, "МКПП", "Бензин", 1.8, 1.8, 145, 145)  # конфликт -> update
    db.commit()
    rows = db._conn.execute("SELECT * FROM configurations").fetchall()
    assert len(rows) == 1
    assert rows[0]["power_from"] == 145
    assert db.get_metadata("schema_version") == "2"
    db.close()


def test_real_db_migrated_in_place():
    """Рабочая база data/aprice.db мигрирована: schema_version=2, UNIQUE есть, bak-v1 рядом."""
    db_file = constants.DB_FILE
    if not db_file.exists():
        pytest.skip("нет рабочей базы")
    conn = sqlite3.connect(str(db_file))
    try:
        assert migrations.get_schema_version(conn) == 2
        assert migrations.has_configurations_unique_index(conn)
    finally:
        conn.close()
    assert Path(str(db_file) + ".bak-v1").exists()


# ---------------------------------------------------------------------------
# Критерий 1.4: limit_brands из конфига
# ---------------------------------------------------------------------------
def test_limit_brands_from_config(tmp_config):
    """limit_brands читается из settings.json; null = без лимита; аргумент приоритетнее."""
    tmp_config.set("builder", "limit_brands", 7)
    p = CatalogParser(db=None, config=tmp_config)  # type: ignore[arg-type]
    assert p._limit == 7

    tmp_config.set("builder", "limit_brands", None)
    p = CatalogParser(db=None, config=tmp_config)  # type: ignore[arg-type]
    assert p._limit is None

    p = CatalogParser(db=None, config=tmp_config, limit_brands=3)  # type: ignore[arg-type]
    assert p._limit == 3
    p = CatalogParser(db=None, config=tmp_config, limit_brands=None)  # type: ignore[arg-type]
    assert p._limit is None


def test_token_from_config(tmp_config):
    """Токен cars-base читается из конфига (п. 6 промпта)."""
    tmp_config.set("builder", "cars_base_token", "secret123")
    p = CatalogParser(db=None, config=tmp_config)  # type: ignore[arg-type]
    url = p._decorate_url("https://api.cars-base.ru/full")
    assert "token=secret123" in url
    assert p._decorate_url("https://auto.ru/cars/all/") == "https://auto.ru/cars/all/"


# ---------------------------------------------------------------------------
# Критерии 5 + 6: WindowManager (aboutToQuit, реестр, компонентный INI)
# ---------------------------------------------------------------------------
def test_window_manager_about_to_quit_and_registry(tmp_path: Path):
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])
    ini = tmp_path / "wm.ini"
    wm = WindowManager(settings=QSettings(str(ini), QSettings.Format.IniFormat))
    win = QWidget()
    win.resize(640, 480)
    win.show()
    wm.apply(win, "test_window")
    assert "test_window" in wm._registry

    # Сохранение мимо closeEvent: эмулируем сигнал завершения приложения
    # (QApplication.quit() / закрытие через трей).
    win.move(123, 45)
    app.aboutToQuit.emit()
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    assert int(settings.value("windows/test_window/geometry/x")) == 123
    assert int(settings.value("windows/test_window/geometry/y")) == 45


def test_window_manager_settings_filename(tmp_path: Path, monkeypatch):
    """Разные settings_filename → независимые хранилища позиций (п. 2.1).

    Примечание: окно не показывается через ``show()`` — в offscreen-среде
    Linux с Qt 6.10 это приводит к crash-on-teardown (артефакт среды CI,
    на Windows тест проходит со show()). Логика сохранения геометрии
    от этого не зависит: closeEvent отрабатывает и для скрытого окна.
    """
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(constants, "CONFIG_DIR", tmp_path)

    wm_b = WindowManager(settings_filename=constants.BUILDER_WINDOW_SETTINGS_FILE)
    win = QWidget()
    win.resize(640, 480)
    win.move(11, 22)
    wm_b.apply(win, "builder_main")
    win.close()
    app.processEvents()  # отработать deleteLater из closeEvent-обработчика WM

    assert (tmp_path / constants.BUILDER_WINDOW_SETTINGS_FILE).exists()

    WindowManager(settings_filename=constants.SERVER_WINDOW_SETTINGS_FILE)
    s = QSettings(
        str(tmp_path / constants.SERVER_WINDOW_SETTINGS_FILE), QSettings.Format.IniFormat
    )
    # Server-хранилище не содержит позиций Builder: окна компонентов независимы.
    assert not s.contains("windows/builder_main/geometry/x")


def test_legacy_ini_renamed(tmp_path: Path, monkeypatch):
    """Старый общий aprice_settings.ini переименовывается в билдерский (п. 2.1)."""
    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(constants, "CONFIG_DIR", tmp_path)
    legacy = tmp_path / constants.LEGACY_WINDOW_SETTINGS_FILE
    legacy.write_text("[windows]\n", encoding="utf-8")

    WindowManager(settings_filename=constants.BUILDER_WINDOW_SETTINGS_FILE)
    assert not legacy.exists()
    assert (tmp_path / constants.BUILDER_WINDOW_SETTINGS_FILE).exists()


# ---------------------------------------------------------------------------
# Критерий 7: логгер для произвольного компонента
# ---------------------------------------------------------------------------
def test_logger_component_server(tmp_path: Path, monkeypatch):
    from common.logger import setup_logging

    monkeypatch.setattr(constants, "LOGS_DIR", tmp_path)
    setup_logging(component="server")
    assert (tmp_path / "aprice_server.log").exists()
    assert (tmp_path / "aprice_server_errors.log").exists()


# ---------------------------------------------------------------------------
# Критерий 8: константы IPC/Server объявлены
# ---------------------------------------------------------------------------
def test_server_constants_declared():
    assert constants.IPC_HOST == "127.0.0.1"
    assert constants.IPC_PORT == 8765
    assert constants.IPC_TIMEOUT_SEC == 5.0
    assert constants.DEFAULT_SERVER_PAGE_DELAY_SEC == [120, 300]
    assert constants.DEFAULT_SERVER_SITE_DELAY_SEC == [600, 1200]
    assert constants.DEFAULT_SERVER_PAGES_PER_SITE == 10
    assert constants.DEFAULT_SERVER_ADS_PER_SITE == 500
    assert constants.SITES == ["auto.ru", "drom.ru", "avito.ru", "sberauto.com", "bibika.ru"]
    assert constants.SITE_SBERAUTO_DISABLED_BY_DEFAULT is True
    assert constants.SCHEMA_VERSION == "2"
