"""Общие константы проекта «АвтоПрайс» (APrice).

Здесь собраны пути, имена файлов, версии схемы и значения по умолчанию,
используемые всеми компонентами комплекса (Builder / Server / Client).
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Версии и имена
# ---------------------------------------------------------------------------
APP_NAME: str = "АвтоПрайс"
APP_NAME_ASCII: str = "APrice"
BUILDER_TITLE: str = "АвтоПрайс — Builder v0.1"
# Актуальная версия схемы БД справочников (миграции — builder/migrations.py).
SCHEMA_VERSION: str = "2"
# Минимальная поддерживаемая версия: более старые схемы мигрируются автоматически.
MIN_SUPPORTED_SCHEMA_VERSION: int = 1
SOURCE_AUTO_RU: str = "auto.ru"
SOURCE_CARS_BASE: str = "cars-base.ru"

# ---------------------------------------------------------------------------
# Пути (относительно корня проекта / каталога .exe)
# ---------------------------------------------------------------------------
def _project_root() -> Path:
    """Возвращает корень проекта или каталог .exe (для PyInstaller-сборки).

    Returns:
        Path: базовый каталог для data/, config/, logs/.
    """
    import sys

    if getattr(sys, "frozen", False):  # собранное приложение (этап 5)
        return Path(sys.executable).parent
    # common/constants.py -> common -> корень проекта
    return Path(__file__).resolve().parent.parent


ROOT_DIR: Path = _project_root()
DATA_DIR: Path = ROOT_DIR / "data"
CACHE_DIR: Path = DATA_DIR / "cache"
CONFIG_DIR: Path = ROOT_DIR / "config"
LOGS_DIR: Path = ROOT_DIR / "logs"

DB_FILE: Path = DATA_DIR / "aprice.db"
SERVER_STATE_DB = DATA_DIR / "aprice_server_state.db"
SETTINGS_FILE: Path = CONFIG_DIR / "settings.json"

# INI-файлы позиций окон (WindowManager): у каждого компонента — свой файл,
# чтобы геометрии окон Builder / Server / Client не пересекались (задача №2, п. 2.1).
BUILDER_WINDOW_SETTINGS_FILE: str = "aprice_builder_settings.ini"
SERVER_WINDOW_SETTINGS_FILE: str = "aprice_server_settings.ini"
CLIENT_WINDOW_SETTINGS_FILE: str = "aprice_client_settings.ini"
# Устаревшее общее имя (до задачи №2) — используется для миграции INI-файла.
LEGACY_WINDOW_SETTINGS_FILE: str = "aprice_settings.ini"

BUILDER_LOG_FILE: Path = LOGS_DIR / "aprice_builder.log"
BUILDER_ERRORS_LOG_FILE: Path = LOGS_DIR / "aprice_builder_errors.log"

# ---------------------------------------------------------------------------
# Сеть / парсинг
# ---------------------------------------------------------------------------
AUTORU_CATALOG_URL: str = "https://auto.ru/cars/all/"
AUTORU_CAPTCHA_MARKER: str = "showcaptcha"

CARS_BASE_FULL_URL: str = "https://api.cars-base.ru/full"
CARS_BASE_GENERATIONS_URL: str = "https://api.cars-base.ru/generations"
CARS_BASE_CONFIGURATIONS_URL: str = "https://api.cars-base.ru/configurations"
CARS_BASE_MODIFICATIONS_URL: str = "https://api.cars-base.ru/modifications"
# Публичный тестовый токен, указанный самим сервисом в сообщении об ошибке 401.
# Значение по умолчанию для settings.json (builder.cars_base_token); при
# переходе на постоянный ключ меняется только конфиг, не код (задача №2, п. 6).
CARS_BASE_TOKEN: str = "test"

HTTP_TIMEOUT_SEC: float = 30.0
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CH_UA: str = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'

# ---------------------------------------------------------------------------
# IPC (подготовка к задаче №3 — APrice-Server; в Builder не используется)
# ---------------------------------------------------------------------------
IPC_HOST: str = "127.0.0.1"
IPC_PORT: int = 8765
IPC_TIMEOUT_SEC: float = 5.0

# ---------------------------------------------------------------------------
# Server: паузы и объёмы по умолчанию (подготовка к задаче №3)
# ---------------------------------------------------------------------------
DEFAULT_SERVER_PAGE_DELAY_SEC: list[int] = [120, 300]      # 2–5 мин между страницами
# DEPRECATED: not used in round-robin (3.4a rev 2) — паузы между площадками
# заменены равномерным сдвигом первых запросов (avg_delay / N).
# Единственное применение (3.4b): пауза перед первым запросом задания,
# восстановленного после перезапуска (TaskManager.restore).
DEFAULT_SERVER_SITE_DELAY_SEC: list[int] = [600, 1200]     # 10–20 мин между площадками
DEFAULT_SERVER_PAGES_PER_SITE: int = 10
DEFAULT_SERVER_ADS_PER_SITE: int = 500

# ---------------------------------------------------------------------------
# Server: площадки (подготовка к задаче №3)
# ---------------------------------------------------------------------------
SITES: list[str] = ["auto.ru", "drom.ru", "avito.ru", "sberauto.com", "bibika.ru"]
SITE_SBERAUTO_DISABLED_BY_DEFAULT: bool = True

# ---------------------------------------------------------------------------
# Настройки по умолчанию (пишутся в settings.json при первом запуске)
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict = {
    "builder": {
        "request_delay_sec": [1, 3],
        "cache_ttl_hours": 168,
        "source": SOURCE_AUTO_RU,
        # Лимит марок за прогон валидации (задача №2, п. 1.4).
        # null — обрабатывать все доступные марки.
        "limit_brands": 5,
        # Токен API cars-base.ru (задача №2, п. 6): по умолчанию публичный
        # тестовый ключ; при получении постоянного — менять только конфиг.
        "cars_base_token": CARS_BASE_TOKEN,
        # Режим браузера Playwright для HTML-фолбэка (задача №2, п. 5
        # ревью): false — запускать видимое окно Chromium.
        "headless": True,
    },
    "logging": {
        "level": "INFO",
        "max_file_size_mb": 10,
        "backup_count": 3,
    },
}

# ---------------------------------------------------------------------------
# QSettings (позиции окон, W-01..W-06)
# ---------------------------------------------------------------------------
QSETTINGS_ORG: str = "APrice"
QSETTINGS_APP: str = "APrice"

# ---------------------------------------------------------------------------
# Миграции БД (задача №2, п. 1.3)
# ---------------------------------------------------------------------------
# Резервная копия БД v1 перед миграцией к схеме v2 (data/aprice.db.bak-v1).
DB_BACKUP_V1_SUFFIX: str = ".bak-v1"
