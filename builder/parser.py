"""Парсер каталога автомобилей для APrice-Builder.

Стратегия (см. отчёт об исследовании в README.md, п. 4.1 ТЗ):

1. **Основной источник** — API ``https://api.cars-base.ru`` (данные каталога
   auto.ru: марки → модели → поколения → модификации). Эндпоинты ``/full``,
   ``/generations``, ``/configurations``, ``/modifications`` доступны без
   регистрации (используется публичный ``?token=test``), отдают чистый JSON
   и не требуют браузерного стека.
2. **Запасной источник** — HTML-каталог ``auto.ru`` через Playwright (Chromium)
   с stealth-патчами (P-01..P-03). Включается автоматически, если API недоступен.

Реализованные требования:

* P-01/P-02 — Playwright + антидетект-инъекции, реалистичный UA/Sec-CH-UA;
* P-03 — abort ресурсов image/media/font/stylesheet;
* P-04 — приоритет API, фолбэк HTML;
* P-05 — только структурные данные каталога, объявления не загружаются;
* P-06 — кэш HTTP-ответов в ``data/cache/`` (JSON, TTL из settings.json);
* P-07 — паузы 1–3 сек между сетевыми запросами;
* P-08 — CAPTCHA/блокировка → лог + остановка с понятным сообщением;
* P-09 — инкрементальное обновление через UPSERT (см. builder/database.py);
* P-10 — прогресс «Обработано марок: X/Y», «Модель BMW X5: найдено N поколений».

Ограничение объёма (комментарий ТЗ-разработчика): по умолчанию обрабатывается
подset популярных марок (``limit_brands``), полный обход включается параметром.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from builder.database import Database
from common import constants
from common.config import Config

logger = logging.getLogger(__name__)

# Тип колбэка прогресса: (текущий шаг, общий шаг или -1, текст сообщения).
ProgressCallback = Callable[[int, int, str], None]


class _Unset:
    """Служебный маркер «параметр не передан» для различения ``None``."""

    def __repr__(self) -> str:  # noqa: D105
        return "<UNSET>"


_UNSET = _Unset()


class CaptchaDetectedError(RuntimeError):
    """Ошибка, сигназирующая о CAPTCHA/блокировке со стороны источника (P-08)."""


@dataclass
class GenerationData:
    """Поколение/рестайлинг модели с набором конфигураций."""

    name: str
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    source_id: Optional[str] = None
    configurations: list[dict] = field(default_factory=list)


@dataclass
class ModelData:
    """Модель автомобиля со списком поколений."""

    name: str
    source_id: Optional[str] = None
    generations: list[GenerationData] = field(default_factory=list)


@dataclass
class BrandData:
    """Марка автомобиля со списком моделей."""

    name: str
    source_id: Optional[str] = None
    models: list[ModelData] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Разбор строки модификации вида «35i 3.0 AT (306 л.с.) 4WD»
# ---------------------------------------------------------------------------
_VOLUME_RE = re.compile(r"(?<![\d.])(\d+[.,]\d)\b", re.IGNORECASE)
_POWER_RE = re.compile(r"\((\d{2,4})\s*л\.?\s*с", re.IGNORECASE)

_TRANSMISSION_MAP: dict[str, str] = {
    "AT": "АКПП",
    "AMT": "Робот",
    "DSG": "DSG",
    "CVT": "Вариатор",
    "MT": "МКПП",
}

_FUEL_MAP: dict[str, str] = {
    "DT": "Дизель",
    "PET": "Бензин",
    "EV": "Электро",
    "PHEV": "Гибрид",
    "HEV": "Гибрид",
    "CNG": "Газ",
    "LPG": "Газ",
}


def parse_modification_name(name: str) -> dict:
    """Извлекает КПП/топливо/объём/мощность из названия модификации.

    Args:
        name: Строка вида ``"xDrive30d 3.0 DT (249 л.с.) 4WD"``.

    Returns:
        dict: ключи ``transmission``, ``fuel_type``, ``volume``, ``power``
        (значения могут быть ``None``).
    """
    result: dict = {
        "transmission": None,
        "fuel_type": None,
        "volume": None,
        "power": None,
    }
    tokens = re.findall(r"[A-Za-zА-Яа-я0-9]+", name)
    for token in tokens:
        upper = token.upper()
        if upper in _TRANSMISSION_MAP and result["transmission"] is None:
            result["transmission"] = _TRANSMISSION_MAP[upper]
        if upper in _FUEL_MAP and result["fuel_type"] is None:
            result["fuel_type"] = _FUEL_MAP[upper]
    vol_match = _VOLUME_RE.search(name)
    if vol_match:
        result["volume"] = float(vol_match.group(1).replace(",", "."))
    power_match = _POWER_RE.search(name)
    if power_match:
        result["power"] = int(power_match.group(1))
    return result


def aggregate_configurations(mods: list[dict]) -> list[dict]:
    """Группирует список модификаций поколения в конфигурации БД.

    Одна запись ``configurations`` = уникальная пара (КПП, топливо);
    диапазоны объёма/мощности вычисляются как min/max по группе.

    Args:
        mods: Список результатов :func:`parse_modification_name`.

    Returns:
        list[dict]: записи для :meth:`Database.upsert_configuration`
        (без ``generation_id`` и ``updated_at``).
    """
    groups: dict[tuple, dict] = {}
    for mod in mods:
        key = (mod["transmission"], mod["fuel_type"])
        group = groups.setdefault(
            key,
            {
                "transmission": mod["transmission"],
                "fuel_type": mod["fuel_type"],
                "volumes": [],
                "powers": [],
            },
        )
        if mod["volume"] is not None:
            group["volumes"].append(mod["volume"])
        if mod["power"] is not None:
            group["powers"].append(mod["power"])

    configs: list[dict] = []
    for group in groups.values():
        volumes, powers = group["volumes"], group["powers"]
        configs.append(
            {
                "transmission": group["transmission"],
                "fuel_type": group["fuel_type"],
                "engine_volume_from": min(volumes) if volumes else None,
                "engine_volume_to": max(volumes) if volumes else None,
                "power_from": min(powers) if powers else None,
                "power_to": max(powers) if powers else None,
            }
        )
    return configs


# ---------------------------------------------------------------------------
# Кэш HTTP-ответов (P-06)
# ---------------------------------------------------------------------------
class ResponseCache:
    """Дисковый JSON-кэш ответов по хэшу URL с TTL из settings.json."""

    def __init__(self, directory: Path, ttl_hours: float) -> None:
        """Инициализирует кэш.

        Args:
            directory: Каталог ``data/cache/``.
            ttl_hours: Срок жизни записи (``builder.cache_ttl_hours``).
        """
        self._dir = directory
        self._ttl_sec = float(ttl_hours) * 3600.0
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self._dir / f"{digest}.json"

    def get(self, url: str) -> Optional[object]:
        """Возвращает закэшированный ответ или ``None`` (промах/протух)."""
        path = self._path_for(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Кэш-файл %s повреждён: %s", path.name, exc)
            return None
        cached_at = float(payload.get("cached_at", 0))
        if time.time() - cached_at > self._ttl_sec:
            logger.debug("Кэш по URL устарел: %s", url)
            return None
        return payload.get("data")

    def put(self, url: str, data: object) -> None:
        """Сохраняет ответ в кэш."""
        path = self._path_for(url)
        payload = {"url": url, "cached_at": time.time(), "data": data}
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Не удалось записать кэш %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Основной парсер
# ---------------------------------------------------------------------------
class CatalogParser:
    """Парсер каталога авто-марок/моделей/поколений с записью в SQLite."""

    def __init__(
        self,
        db: Database,
        config: Config,
        progress_cb: Optional[ProgressCallback] = None,
        stop_event: Optional[threading.Event] = None,
        limit_brands: object = _UNSET,
        cache_dir: Optional[Path] = None,
    ) -> None:
        """Инициализирует парсер.

        Args:
            db: Открытое соединение со схемой.
            config: Загруженная конфигурация (паузы, TTL кэша, источник).
            progress_cb: Колбэк прогресса ``(step, total, message)``;
                ``total == -1`` означает неизвестное общее число шагов.
            stop_event: Флаг остановки («Остановить» в GUI).
            limit_brands: Максимум марок за прогон. Если параметр не
                передан — значение читается из ``settings.json``
                (``builder.limit_brands``, ``null`` = без лимита);
                явный ``None`` также означает «без лимита».
            cache_dir: Каталог JSON-кэша ответов (P-06). По умолчанию —
                ``data/cache/``; параметр позволяет изолировать кэш
                (тесты, несколько экземпляров парсера).
        """
        self._db = db
        self._config = config
        self._progress = progress_cb or (lambda *_: None)
        self._stop = stop_event or threading.Event()

        # Лимит марок: явный аргумент приоритетнее конфига; в значениях по
        # умолчанию лимит берётся из settings.json (builder.limit_brands,
        # null = без лимита) — задача №2, п. 1.4.
        if limit_brands is _UNSET:
            raw_limit = config.get("builder", "limit_brands", 5)
            self._limit: Optional[int] = int(raw_limit) if raw_limit is not None else None
        elif limit_brands is None:
            # Явно переданный None трактуем как «без лимита» только через
            # sentinel _UNSET; для обратной совместимости None == без лимита.
            self._limit = None
        else:
            self._limit = int(limit_brands)

        # Токен API cars-base.ru из конфига (задача №2, п. 6).
        self._token = str(config.get("builder", "cars_base_token", constants.CARS_BASE_TOKEN))
        # Режим браузера HTML-фолбэка (задача №2, п. 5 ревью).
        self._headless = bool(config.get("builder", "headless", True))

        delay = config.get("builder", "request_delay_sec", [1, 3])
        lo, hi = (float(delay[0]), float(delay[1])) if len(delay) >= 2 else (1.0, 3.0)
        self._delay_range = (lo, hi)

        # Каталог кэша: по умолчанию data/cache/ рядом с проектом (P-06);
        # аргумент cache_dir позволяет изолировать кэш (тесты, несколько
        # экземпляров парсера).
        self._cache = ResponseCache(
            Path(cache_dir) if cache_dir is not None else constants.CACHE_DIR,
            float(config.get("builder", "cache_ttl_hours", 168)),
        )

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Выполняет обновление справочной базы (приоритет — API, фолбэк — HTML).

        Raises:
            CaptchaDetectedError: источник заблокировал запрос (P-08).
        """
        source = str(self._config.get("builder", "source", constants.SOURCE_AUTO_RU))
        logger.info("Начинаю обновление каталога. Источник: %s", source)
        brands: list[BrandData] | None = None
        used_source = ""

        try:
            brands = self._fetch_via_cars_base_api()
            used_source = constants.SOURCE_CARS_BASE
        except CaptchaDetectedError:
            raise
        except Exception as exc:  # noqa: BLE001 — переходим к фолбэку
            logger.warning("API cars-base.ru недоступен (%s) — пробую HTML auto.ru", exc)

        if brands is None:
            brands = self._fetch_via_autoru_html()
            used_source = constants.SOURCE_AUTO_RU

        self._store(brands)
        self._db.set_metadata("source", used_source)
        self._db.mark_update_finished()
        counts = self._db.counts()
        logger.info(
            "Обновление завершено. Марки: %(manufacturers)s, модели: %(models)s, "
            "поколения: %(generations)s, конфигурации: %(configurations)s",
            counts,
        )

    # ------------------------------------------------------------------
    # Шаг 1. API cars-base.ru (P-04)
    # ------------------------------------------------------------------
    def _fetch_via_cars_base_api(self) -> list[BrandData]:
        """Загружает марки/модели (/full), поколения и модификации по API.

        Порядок обхода (п. 4.3 ТЗ): список марок одним запросом → поколения
        одним запросом → модификации по моделям (для марок в лимите).

        Returns:
            list[BrandData]: распарсенный каталог.
        """
        full = self._http_json_cached(constants.CARS_BASE_FULL_URL)
        raw_brands: list[dict] = full.get("data", [])
        if not raw_brands:
            raise RuntimeError("API вернул пустой список марок")

        popular = [b for b in raw_brands if b.get("popular")] or raw_brands
        selected = popular[: self._limit] if self._limit else popular
        logger.info(
            "API /full: всего марок %d, к обработке выбрано %d (лимит %s)",
            len(raw_brands), len(selected), self._limit,
        )

        generations_index = self._build_generations_index()

        brands: list[BrandData] = []
        total = len(selected)
        for idx, brand_raw in enumerate(selected, start=1):
            self._check_stop()
            brand = BrandData(
                name=brand_raw["name"].strip(),
                source_id=str(brand_raw.get("id", "")),
            )
            for model_raw in brand_raw.get("models", []):
                model = ModelData(
                    name=model_raw["name"].strip(),
                    source_id=str(model_raw.get("id", "")),
                )
                gen_rows = generations_index.get(model_raw.get("id", ""), [])
                for gen_raw in gen_rows:
                    gen = GenerationData(
                        name=self._normalize_generation_name(gen_raw),
                        year_from=gen_raw.get("year_from"),
                        year_to=gen_raw.get("year_to"),
                        source_id=str(gen_raw.get("id", "")),
                    )
                    mods = self._fetch_modifications(model_raw["id"], gen.source_id)
                    gen.configurations = aggregate_configurations(
                        [parse_modification_name(m.get("name", "")) for m in mods]
                    )
                    model.generations.append(gen)
                brand.models.append(model)
            logger.info(
                "Марка %s: найдено %d моделей", brand.name, len(brand.models)
            )
            for model in brand.models:
                if model.generations:
                    logger.info(
                        "Модель %s %s: найдено %d поколений",
                        brand.name, model.name, len(model.generations),
                    )
            logger.info("Обработано марок: %d/%d", idx, total)
            self._progress(idx, total, f"Обработано марок: {idx}/{total} ({brand.name})")
            brands.append(brand)
        return brands

    def _build_generations_index(self) -> dict[str, list[dict]]:
        """Загружает все поколения одним запросом и индексирует по model_id."""
        data = self._http_json_cached(constants.CARS_BASE_GENERATIONS_URL)
        index: dict[str, list[dict]] = {}
        for row in data.get("data", []):
            index.setdefault(row.get("model_id", ""), []).append(row)
        logger.info("API /generations: получено поколений: %d", len(data.get("data", [])))
        return index

    def _fetch_modifications(
        self, model_id: str, generation_id: Optional[str]
    ) -> list[dict]:
        """Возвращает модификации модели (или одного поколения) с кэшем."""
        params = {"model_id": model_id}
        if generation_id:
            params["generation_id"] = generation_id
        url = (
            constants.CARS_BASE_MODIFICATIONS_URL
            + "?"
            + urllib.parse.urlencode(params)
        )
        try:
            data = self._http_json_cached(url)
        except CaptchaDetectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Модификации %s: ошибка %s — пропускаю", model_id, exc)
            return []
        rows = data.get("data", [])
        if generation_id:
            rows = [r for r in rows if str(r.get("generation_id")) == str(generation_id)]
        return rows

    @staticmethod
    def _normalize_generation_name(gen_raw: dict) -> str:
        """Формирует человекочитаемое имя поколения («IV рестайлинг», «2018–2023»)."""
        name = (gen_raw.get("name") or "").strip()
        year_from = gen_raw.get("year_from")
        year_to = gen_raw.get("year_to")
        if name:
            if year_from or year_to:
                years = f"{year_from or '?'}–{year_to or '?'}"
                return f"{name} ({years})"
            return name
        if year_from or year_to:
            return f"{year_from or '?'}–{year_to or '?'}"
        return "Без поколения"

    # ------------------------------------------------------------------
    # Шаг 2. HTML-фолбэк через Playwright (P-01..P-03)
    # ------------------------------------------------------------------
    def _fetch_via_autoru_html(self) -> list[BrandData]:
        """Парсит HTML-каталог auto.ru через Playwright (запасной путь).

        Используется только если API недоступен. Извлекает ссылки марок/моделей
        со страниц каталога; при обнаружении CAPTCHA бросает
        :class:`CaptchaDetectedError` (P-08).

        Перед запуском браузера выполняется предпроверка наличия Chromium для
        Playwright (задача №2, п. 1.1): если браузер не установлен — бросается
        :class:`RuntimeError` с инструкцией ``playwright install chromium``;
        сообщение доставляется в GUI через ``failed.emit`` без трейсбека.

        Raises:
            RuntimeError: Playwright или Chromium не установлены / фолбэк
                недоступен.
            CaptchaDetectedError: auto.ru показал CAPTCHA (P-08).
        """
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright не установлен — выполните "
                "'pip install playwright && playwright install chromium'"
            ) from exc

        blocked_types = {"image", "media", "font", "stylesheet"}
        brands: list[BrandData] = []
        with sync_playwright() as pw:
            # П. 1.1: предпроверка установленного браузера до launch().
            try:
                executable = pw.chromium.executable_path
            except Exception as exc:  # noqa: BLE001 — трактуем как отсутствие браузера
                raise RuntimeError(
                    "Браузер Chromium для Playwright не установлен. "
                    "Выполните в командной строке: playwright install chromium"
                ) from exc
            if not executable or not Path(executable).exists():
                raise RuntimeError(
                    "Браузер Chromium для Playwright не установлен. "
                    "Выполните в командной строке: playwright install chromium"
                )

            browser = pw.chromium.launch(headless=self._headless)
            context = browser.new_context(
                user_agent=constants.USER_AGENT,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "sec-ch-ua": constants.CH_UA,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            # P-01: stealth-патчи на уровне всей страницы.
            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',
                    {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages',
                    {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
                window.chrome = { runtime: {} };
                """
            )
            page = context.new_page()

            def _route_filter(route) -> None:  # noqa: ANN001
                # P-03: отсекаем тяжёлые ресурсы.
                if route.request.resource_type in blocked_types:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", _route_filter)

            url = constants.AUTORU_CATALOG_URL
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            final_url = page.url
            if constants.AUTORU_CAPTCHA_MARKER in final_url:
                browser.close()
                raise CaptchaDetectedError(
                    "auto.ru показал CAPTCHA — парсинг прекращён. "
                    "Повторите позже или используйте основной API-источник."
                )

            anchors = page.eval_on_selector_all(
                'a[href*="/cars/"]',
                "els => els.map(e => ({href: e.getAttribute('href'), "
                "text: e.textContent.trim()}))",
            )
            browser.close()

        seen: set[str] = set()
        for item in anchors or []:
            href = (item.get("href") or "").split("?")[0]
            text = item.get("text") or ""
            match = re.match(r"^/cars/([a-z0-9\-]+)/?$", href)
            if match and text and text.lower() not in seen:
                seen.add(text.lower())
                brands.append(BrandData(name=text, source_id=match.group(1)))
            if self._limit and len(brands) >= self._limit:
                break

        if not brands:
            raise RuntimeError(
                "HTML-каталог auto.ru не содержит распознаваемых марок "
                "(возможна блокировка или изменение вёрстки)"
            )
        logger.info("HTML-фолбэк: найдено марок: %d", len(brands))
        self._progress(len(brands), len(brands), "HTML-каталог загружен")
        return brands

    # ------------------------------------------------------------------
    # Шаг 3. Запись в БД (P-09)
    # ------------------------------------------------------------------
    def _store(self, brands: list[BrandData]) -> None:
        """Инкрементально записывает каталог в SQLite (UPSERT)."""
        total = len(brands)
        for idx, brand in enumerate(brands, start=1):
            self._check_stop()
            man_id = self._db.upsert_manufacturer(brand.name, brand.source_id)
            for model in brand.models:
                mod_id = self._db.upsert_model(man_id, model.name, model.source_id)
                for gen in model.generations:
                    gen_id = self._db.upsert_generation(
                        mod_id,
                        gen.name,
                        gen.year_from,
                        gen.year_to,
                        gen.source_id,
                    )
                    for conf in gen.configurations:
                        self._db.upsert_configuration(gen_id, **conf)
            self._db.commit()
            logger.info("Обработано марок: %d/%d (запись в БД)", idx, total)
            self._progress(idx, total, f"Запись в БД: {idx}/{total}")

    # ------------------------------------------------------------------
    # HTTP с кэшем, паузами и детектом блокировок
    # ------------------------------------------------------------------
    def _http_json_cached(self, url: str) -> dict:
        """Возвращает JSON по URL, пользуясь дисковым кэшем (P-06).

        Сетевой запрос сопровождается паузой 1–3 с (P-07). Детект блокировок
        (P-08, задача №2 п. 1.2 — без ложных срабатываний):

        * ``Content-Type: application/json`` → тело всегда парсится как JSON,
          слово «captcha» в тексте игнорируется;
        * не-JSON тело → CAPTCHA считается найденной, если в итоговом URL
          (с учётом редиректов) есть ``showcaptcha``/``smartcaptcha``, либо в
          первых 500 символах тела встречается ``showcaptcha``;
        * HTTP 403/429 → :class:`CaptchaDetectedError`; прочие коды и
          битый JSON → :class:`RuntimeError` с указанием URL и кода.

        Args:
            url: Полный URL эндпоинта (без токена — он добавляется здесь).

        Returns:
            dict: разобранный JSON-ответ.

        Raises:
            CaptchaDetectedError: реальная CAPTCHA/блокировка источника.
            RuntimeError: сетевая/HTTP-ошибка или некорректный ответ.
        """
        request_url = self._decorate_url(url)
        cached = self._cache.get(request_url)
        if cached is not None:
            logger.debug("Кэш-хит: %s", url)
            return cached  # type: ignore[return-value]

        self._check_stop()
        self._sleep_polite()
        request = urllib.request.Request(
            request_url,
            headers={
                "User-Agent": constants.USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — фиксированный HTTPS-хост
                request, timeout=constants.HTTP_TIMEOUT_SEC
            ) as response:
                status = response.status
                content_type = (response.headers.get("Content-Type") or "").lower()
                final_url = response.geturl()
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise CaptchaDetectedError(
                    f"Источник вернул {exc.code} — вероятная блокировка. "
                    "Парсинг прекращён."
                ) from exc
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Сетевая ошибка при запросе {url}: {exc}") from exc

        if status != 200:
            raise RuntimeError(f"HTTP {status} при запросе {url}")

        is_json_ct = "application/json" in content_type
        if not is_json_ct and self._looks_like_captcha(final_url, body):
            raise CaptchaDetectedError(
                "В ответе обнаружена страница CAPTCHA — парсинг прекращён."
            )

        try:
            data = json.loads(body.lstrip("\ufeff \t\r\n"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Некорректный JSON от {url}: {exc}") from exc

        self._cache.put(request_url, data)
        return data

    @staticmethod
    def _looks_like_captcha(final_url: str, body: str) -> bool:
        """Эвристика детекта CAPTCHA для НЕ-json ответов (задача №2, п. 1.2).

        Проверяет итоговый URL после редиректов на маркеры ``showcaptcha`` /
        ``smartcaptcha`` и начало тела страницы. Для ответов с
        ``Content-Type: application/json`` не вызывается — это исключает
        ложные срабатывания на легальном тексте «captcha» в данных.

        Args:
            final_url: URL ответа после всех редиректов.
            body: Текстовое тело ответа.

        Returns:
            bool: ``True``, если ответ похож на страницу CAPTCHA/блокировки.
        """
        lowered_url = (final_url or "").lower()
        if "showcaptcha" in lowered_url or "smartcaptcha" in lowered_url:
            return True
        head = body[:500].lower()
        return "showcaptcha" in head

    def _decorate_url(self, url: str) -> str:
        """Добавляет токен к эндпоинтам cars-base.ru (значение — из конфига).

        Токен читается из ``settings.json`` (``builder.cars_base_token``,
        по умолчанию публичный тестовый ключ ``"test"``): при переходе на
        постоянный ключ меняется только конфиг, не код (задача №2, п. 6).

        Args:
            url: URL эндпоинта без токена.

        Returns:
            str: URL с параметром ``token`` (для сторонних URL — без изменений).
        """
        if "cars-base.ru" not in url:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}token={self._token}"

    def _sleep_polite(self) -> None:
        """Пауза 1–3 секунды между запросами (P-07), прерываемая остановкой."""
        lo, hi = self._delay_range
        remaining = random.uniform(lo, hi)  # noqa: S311 — не для криптографии
        while remaining > 0:
            self._check_stop()
            step = min(0.2, remaining)
            time.sleep(step)
            remaining -= step

    def _check_stop(self) -> None:
        """Проверяет флаг остановки; бросает ``InterruptedError`` при установке."""
        if self._stop.is_set():
            raise InterruptedError("Парсинг остановлен пользователем")
