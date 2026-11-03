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
SCHEMA_VERSION: str = "1"
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
SETTINGS_FILE: Path = CONFIG_DIR / "settings.json"

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
CARS_BASE_TOKEN: str = "test"

HTTP_TIMEOUT_SEC: float = 30.0
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CH_UA: str = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'

# ---------------------------------------------------------------------------
# Настройки по умолчанию (пишутся в settings.json при первом запуске)
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict = {
    "builder": {
        "request_delay_sec": [1, 3],
        "cache_ttl_hours": 168,
        "source": SOURCE_AUTO_RU,
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
