"""Тесты Builder-fix: полный сбор (без лимита 30 марок), поколения, кэш, «Очистить кэш».

Сеть не используется: ответы API подменяются на уровне ``_http_json_cached`` или
``urllib.request.urlopen``.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from builder import parser as parser_module
from builder.database import Database
from builder.parser import (
    CaptchaDetectedError,
    CatalogParser,
    ResponseCache,
    clear_cache,
    select_brands,
)
from common import constants
from common.config import Config

BRANDS = 429
POPULAR = 30
MODELS_PER_BRAND = 2


# ---------------------------------------------------------------------------
# Поддельное API cars-base.ru
# ---------------------------------------------------------------------------
def make_catalog(model_id_type=int) -> tuple[list[dict], list[dict]]:
    """429 марок (30 «популярных»), по 2 модели, у каждой модели одно поколение."""
    brands, generations = [], []
    model_id = 0
    for b in range(BRANDS):
        models = []
        for _ in range(MODELS_PER_BRAND):
            model_id += 1
            models.append({"id": model_id_type(model_id), "name": f"Model{model_id}"})
            generations.append({"id": str(model_id * 10), "model_id": model_id,
                                "name": "I", "year_from": 2010, "year_to": 2015})
        brands.append({"id": b + 1, "name": f"Brand{b + 1}", "popular": b < POPULAR,
                       "models": models})
    return brands, generations


class FakeApi:
    """Подменяет ``CatalogParser._http_json_cached``; запоминает запросы."""

    def __init__(self, brands, generations, bulk_limit=None, honors_model_id=True):
        self.brands, self.generations = brands, generations
        self.bulk_limit, self.honors_model_id = bulk_limit, honors_model_id
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if base == constants.CARS_BASE_FULL_URL:
            return {"data": self.brands}
        if base == constants.CARS_BASE_GENERATIONS_URL:
            if "model_id" in query:
                if not self.honors_model_id:
                    return {"data": self.generations}
                wanted = query["model_id"][0]
                return {"data": [g for g in self.generations if str(g["model_id"]) == wanted]}
            rows = self.generations if self.bulk_limit is None else self.generations[: self.bulk_limit]
            return {"data": rows}
        if base == constants.CARS_BASE_MODIFICATIONS_URL:
            return {"data": []}
        raise AssertionError(f"неожиданный URL {url}")

    def per_model_requests(self) -> list[str]:
        return [u for u in self.urls
                if u.startswith(constants.CARS_BASE_GENERATIONS_URL + "?")]


@pytest.fixture()
def make_parser(tmp_path: Path):
    def _make(api: FakeApi, config: Config | None = None, **kwargs) -> CatalogParser:
        db = Database(db_path=tmp_path / "t.db")
        db.init_schema()
        cfg = config or Config(path=tmp_path / "settings.json").load()
        parser = CatalogParser(db=db, config=cfg, cache_dir=tmp_path / "cache", **kwargs)
        parser._http_json_cached = api  # type: ignore[method-assign]
        return parser
    return _make


def count_generations(brands) -> int:
    return sum(len(m.generations) for b in brands for m in b.models)


# ---------------------------------------------------------------------------
# 1. Лимит марок
# ---------------------------------------------------------------------------
def test_select_brands_without_limit_returns_all_in_order():
    brands, _ = make_catalog()
    for empty in (None, 0):
        selected = select_brands(brands, empty)
        assert len(selected) == BRANDS and selected == brands  # порядок сохранён


def test_select_brands_with_limit_prefers_popular():
    brands, _ = make_catalog()
    selected = select_brands(brands, 5)
    assert [b["name"] for b in selected] == [f"Brand{i}" for i in range(1, 6)]
    assert all(b["popular"] for b in selected)


def test_select_brands_limit_above_popular_fills_with_the_rest():
    brands, _ = make_catalog()
    selected = select_brands(brands, POPULAR + 10)
    assert len(selected) == POPULAR + 10
    assert sum(1 for b in selected if b["popular"]) == POPULAR
    assert select_brands(brands, 10_000) == brands[:POPULAR] + brands[POPULAR:]


def test_select_brands_without_popular_flags_still_works():
    brands = [{"name": str(i)} for i in range(50)]
    assert len(select_brands(brands, None)) == 50
    assert len(select_brands(brands, 7)) == 7


def test_full_collection_processes_all_429_brands(make_parser, caplog):
    brands, gens = make_catalog()
    parser = make_parser(FakeApi(brands, gens), limit_brands=None)
    with caplog.at_level(logging.INFO, logger="builder.parser"):
        result = parser._fetch_via_cars_base_api()
    assert len(result) == BRANDS  # раньше было 30
    assert sum(len(b.models) for b in result) == BRANDS * MODELS_PER_BRAND
    assert count_generations(result) == BRANDS * MODELS_PER_BRAND
    text = caplog.text
    assert "всего марок 429, к обработке выбрано 429" in text
    assert f"API /generations: получено поколений: {BRANDS * MODELS_PER_BRAND}" in text


def test_limit_from_config_null_means_all_brands(make_parser, tmp_path):
    brands, gens = make_catalog()
    cfg = Config(path=tmp_path / "settings.json").load()
    cfg.set("builder", "limit_brands", None)
    assert len(make_parser(FakeApi(brands, gens), cfg)._fetch_via_cars_base_api()) == BRANDS


def test_limit_five_still_works(make_parser):
    brands, gens = make_catalog()
    result = make_parser(FakeApi(brands, gens), limit_brands=5)._fetch_via_cars_base_api()
    assert [b.name for b in result] == [f"Brand{i}" for i in range(1, 6)]


def test_run_stores_everything_and_logs_summary(make_parser, caplog):
    brands, gens = make_catalog()
    parser = make_parser(FakeApi(brands, gens), limit_brands=None)
    with caplog.at_level(logging.INFO, logger="builder.parser"):
        parser.run()
    counts = parser._db.counts()
    assert counts["manufacturers"] == BRANDS
    assert counts["models"] == BRANDS * MODELS_PER_BRAND
    assert counts["generations"] == BRANDS * MODELS_PER_BRAND
    assert "Обновление завершено. Марки: 429, модели: 858, поколения: 858" in caplog.text


# ---------------------------------------------------------------------------
# 2. Поколения
# ---------------------------------------------------------------------------
def test_generations_matched_when_id_types_differ(make_parser):
    """id модели в /full — строка, model_id в /generations — число (и наоборот)."""
    brands, gens = make_catalog(model_id_type=str)
    api = FakeApi(brands, gens)
    result = make_parser(api, limit_brands=None)._fetch_via_cars_base_api()
    assert count_generations(result) == BRANDS * MODELS_PER_BRAND
    assert api.per_model_requests() == []  # общий список использован полностью


def test_truncated_bulk_list_is_completed_per_model(make_parser, caplog):
    brands, gens = make_catalog()
    api = FakeApi(brands, gens, bulk_limit=50)
    parser = make_parser(api, limit_brands=None)
    with caplog.at_level(logging.INFO, logger="builder.parser"):
        result = parser._fetch_via_cars_base_api()
    total = BRANDS * MODELS_PER_BRAND
    assert count_generations(result) == total
    assert len(api.per_model_requests()) == total - 50  # только для недостающих моделей
    assert "API /generations: получено поколений: 50" in caplog.text
    assert f"из общего списка 50, запросами по моделям {total - 50}" in caplog.text
    assert parser._generations_from_bulk == 50 and parser._generations_per_model == total - 50


def test_per_model_request_filters_foreign_rows_and_disables_itself(make_parser, caplog):
    brands, gens = make_catalog()
    api = FakeApi(brands, gens, bulk_limit=50, honors_model_id=False)
    parser = make_parser(api, limit_brands=None)
    with caplog.at_level(logging.WARNING, logger="builder.parser"):
        result = parser._fetch_via_cars_base_api()
    assert len(api.per_model_requests()) == 1  # API игнорирует model_id — больше не спрашиваем
    assert "игнорирует model_id" in caplog.text
    total = BRANDS * MODELS_PER_BRAND
    # 50 из общего списка + одна «своя» строка из первого per-model ответа
    assert count_generations(result) == 51 < total
    assert parser._per_model_generations_enabled is False


def test_per_model_requests_can_be_disabled_in_config(make_parser, tmp_path):
    brands, gens = make_catalog()
    cfg = Config(path=tmp_path / "settings.json").load()
    cfg.set("builder", "generations_per_model", False)
    api = FakeApi(brands, gens, bulk_limit=50)
    result = make_parser(api, cfg, limit_brands=None)._fetch_via_cars_base_api()
    assert api.per_model_requests() == [] and count_generations(result) == 50


def test_per_model_error_is_skipped_but_captcha_propagates(make_parser):
    brands, gens = make_catalog()

    class Flaky(FakeApi):
        mode = "error"

        def __call__(self, url):
            if "model_id" in url and url.startswith(constants.CARS_BASE_GENERATIONS_URL):
                if self.mode == "captcha":
                    raise CaptchaDetectedError("блокировка")
                raise RuntimeError("HTTP 500")
            return super().__call__(url)

    api = Flaky(brands, gens, bulk_limit=2)
    result = make_parser(api, limit_brands=3)._fetch_via_cars_base_api()
    assert len(result) == 3  # ошибка отдельной модели не обрывает сбор
    api.mode = "captcha"
    with pytest.raises(CaptchaDetectedError):
        make_parser(api, limit_brands=3)._fetch_via_cars_base_api()


def test_model_without_generations_gets_no_generations(make_parser):
    brands, gens = make_catalog()
    gens = [g for g in gens if g["model_id"] != 1]  # у модели 1 поколений нет вовсе
    api = FakeApi(brands, gens)
    result = make_parser(api, limit_brands=1)._fetch_via_cars_base_api()
    assert result[0].models[0].generations == []
    assert result[0].models[1].generations != []


# ---------------------------------------------------------------------------
# 3. Кэш
# ---------------------------------------------------------------------------
class FakeHttp:
    def __init__(self, body: str):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._body = body.encode()

    def read(self):
        return self._body

    def geturl(self):
        return "https://api.cars-base.ru/x"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def real_parser(tmp_path):
    db = Database(db_path=tmp_path / "r.db")
    db.init_schema()
    cfg = Config(path=tmp_path / "settings.json").load()
    cfg.set("builder", "request_delay_sec", [0, 0])
    return CatalogParser(db=db, config=cfg, cache_dir=tmp_path / "cache")


def test_empty_list_response_is_not_cached(real_parser, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(1) or FakeHttp('{"data": []}'))
    url = constants.CARS_BASE_GENERATIONS_URL
    real_parser._http_json_cached(url)
    real_parser._http_json_cached(url)
    assert len(calls) == 2  # пустой ответ «не залипает» в кэше
    assert not list((real_parser._cache._dir).glob("*"))


def test_non_empty_response_is_cached(real_parser, monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(1) or FakeHttp('{"data": [{"id": 1}]}'))
    url = constants.CARS_BASE_GENERATIONS_URL
    real_parser._http_json_cached(url)
    real_parser._http_json_cached(url)
    assert len(calls) == 1


def test_empty_per_model_response_is_cached(real_parser, monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(1) or FakeHttp('{"data": []}'))
    url = constants.CARS_BASE_GENERATIONS_URL + "?model_id=7"
    real_parser._http_json_cached(url)
    real_parser._http_json_cached(url)
    assert len(calls) == 1  # у модели без поколений пустой ответ — норма, не долбим повторно


def test_clear_cache_removes_files_only(tmp_path):
    cache = tmp_path / "cache"
    (cache / "sub").mkdir(parents=True)
    (cache / "a.json").write_text("{}")
    (cache / "b.json").write_text("{}")
    (cache / "sub" / "keep.json").write_text("{}")
    assert clear_cache(cache) == 2
    assert [p.name for p in cache.iterdir()] == ["sub"]
    assert (cache / "sub" / "keep.json").exists()
    assert clear_cache(cache) == 0


def test_clear_cache_missing_directory_is_not_an_error(tmp_path):
    assert clear_cache(tmp_path / "nope") == 0


def test_clear_cache_default_is_constants_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "CACHE_DIR", tmp_path / "c")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "x.json").write_text("{}")
    assert clear_cache() == 1


def test_clear_cache_reports_unremovable_file(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.json").write_text("{}")

    def locked(self, *a, **k):
        raise PermissionError("файл занят")

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(OSError):
        clear_cache(cache)


def test_response_cache_clear_method(tmp_path):
    cache = ResponseCache(tmp_path / "rc", ttl_hours=1)
    cache.put("https://x/1", {"data": [1]})
    cache.put("https://x/2", {"data": [2]})
    assert cache.get("https://x/1") is not None
    assert cache.clear() == 2
    assert cache.get("https://x/1") is None


# ---------------------------------------------------------------------------
# 4. Кнопка «Очистить кэш» в окне Builder
# ---------------------------------------------------------------------------
@pytest.fixture()
def builder_window(qapp, tmp_path, monkeypatch):
    from builder import gui

    db_path = tmp_path / "gui.db"
    monkeypatch.setattr(gui, "Database", lambda *a, **k: Database(db_path=db_path))
    monkeypatch.setattr(constants, "CACHE_DIR", tmp_path / "cache")
    window = gui.BuilderWindow(Config(path=tmp_path / "settings.json").load(), logging.NullHandler())
    window.errors = []
    monkeypatch.setattr(window, "_show_error", window.errors.append)
    return window


def test_clear_cache_button_removes_cache_files(builder_window, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("a.json", "b.json", "c.json"):
        (cache / name).write_text("{}")
    assert builder_window.btn_clear_cache.text() == "Очистить кэш"
    builder_window.btn_clear_cache.click()
    assert list(cache.iterdir()) == []
    assert "Кэш очищен: удалено файлов — 3" in builder_window.log_view.toPlainText()
    assert builder_window.errors == []


def test_clear_cache_button_with_no_cache_dir(builder_window):
    builder_window.btn_clear_cache.click()
    assert "удалено файлов — 0" in builder_window.log_view.toPlainText()


def test_clear_cache_button_shows_error(builder_window, monkeypatch):
    from builder import gui

    monkeypatch.setattr(gui, "clear_cache", lambda: (_ for _ in ()).throw(OSError("занят")))
    builder_window.btn_clear_cache.click()
    assert builder_window.errors == ["Не удалось очистить кэш: занят"]


def test_clear_cache_button_disabled_during_collection(builder_window):
    assert builder_window.btn_clear_cache.isEnabled()
    builder_window.btn_update.setEnabled(False)
    builder_window.btn_clear_cache.setEnabled(False)
    builder_window._reset_buttons()  # сбор окончен
    assert builder_window.btn_clear_cache.isEnabled() and builder_window.btn_update.isEnabled()
