"""Тесты абстракции парсеров и заглушек 4 площадок (подзадача 3.3).

Сеть и Qt не используются — тесты чистые и быстрые
(``test_fast_mode`` сокращает паузы заглушек до 0.1–0.3 с).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from common.config import Config
from server.parsers import get_parser
from server.parsers.base import (
    Ad,
    BaseSiteParser,
    BlockedError,
    CaptchaDetectedError,
    ParserError,
    UnsupportedSiteError,
)

SUPPORTED_SITES = ["auto.ru", "drom.ru", "avito.ru", "bibika.ru"]


# ----------------------------------------------------------------------
# Фикстуры
# ----------------------------------------------------------------------
@pytest.fixture()
def config(tmp_path) -> Config:
    """Возвращает конфиг с временным settings.json (без чтения рабочей БД)."""
    cfg = Config(path=tmp_path / "settings.json")
    cfg.save()
    return cfg


@pytest.fixture()
def spec() -> dict:
    """Типовая спецификация задания (mode=range)."""
    return {
        "mode": "range",
        "manufacturer": "BMW",
        "model": "X5",
        "year_from": 2018,
        "year_to": 2021,
        "transmissions": ["АКПП"],
        "fuels": ["Бензин"],
    }


def _fast_options(**extra) -> dict:
    """Опции с укороченными паузами + переданные дополнения."""
    options = {
        "sites": SUPPORTED_SITES,
        "max_ads_per_site": 500,
        "max_pages_per_site": 10,
        "test_fast_mode": True,
    }
    options.update(extra)
    return options


# ----------------------------------------------------------------------
# 1. Фабрика
# ----------------------------------------------------------------------
class TestFactory:
    """Проверки get_parser для всех площадок и ошибок."""

    @pytest.mark.parametrize("site", SUPPORTED_SITES)
    def test_factory_returns_parser(self, site: str, config: Config) -> None:
        """Фабрика возвращает BaseSiteParser с правильным site_name."""
        parser = get_parser(site, config)
        assert isinstance(parser, BaseSiteParser)
        assert parser.site_name == site

    def test_factory_sberauto_unsupported(self, config: Config) -> None:
        """sberauto.com -> UnsupportedSiteError (S-15), файла-парсера нет."""
        with pytest.raises(UnsupportedSiteError):
            get_parser("sberauto.com", config)

    def test_factory_unknown_site(self, config: Config) -> None:
        """Неизвестная площадка -> UnsupportedSiteError."""
        with pytest.raises(UnsupportedSiteError):
            get_parser("example.com", config)


# ----------------------------------------------------------------------
# 2. collect возвращает list[Ad]
# ----------------------------------------------------------------------
class TestCollect:
    """Базовое поведение заглушек."""

    @pytest.mark.parametrize("site", SUPPORTED_SITES)
    def test_collect_returns_ads(
        self, site: str, config: Config, spec: dict
    ) -> None:
        """Для каждой площадки — непустой список Ad с url и price."""
        parser = get_parser(site, config)
        ads = parser.collect(spec, _fast_options(), threading.Event())
        assert ads, f"{site}: пустой результат"
        assert all(isinstance(a, Ad) for a in ads)
        for ad in ads:
            assert ad.url
            assert ad.price is not None
            assert site in ad.url
            assert ad.manufacturer == spec["manufacturer"]
            assert ad.model == spec["model"]
            assert spec["year_from"] <= ad.year <= spec["year_to"]

    def test_progress_cb_called(
        self, config: Config, spec: dict
    ) -> None:
        """progress_cb вызывается после каждой страницы с монотонным счётчиком."""
        parser = get_parser("auto.ru", config)
        calls: list[tuple[int, int]] = []
        options = _fast_options(max_pages_per_site=3)
        ads = parser.collect(
            spec, options, threading.Event(),
            progress_cb=lambda p, n: calls.append((p, n)),
        )
        assert len(calls) == 3
        assert [p for p, _ in calls] == [1, 2, 3]
        assert calls[-1][1] == len(ads)


# ----------------------------------------------------------------------
# 3-4. Лимиты
# ----------------------------------------------------------------------
class TestLimits:
    """Соблюдение max_ads_per_site / max_pages_per_site (S-17)."""

    def test_max_ads_limit(self, config: Config, spec: dict) -> None:
        """При max_ads_per_site=5 возвращается ровно <= 5 объявлений."""
        parser = get_parser("drom.ru", config)
        ads = parser.collect(
            spec, _fast_options(max_ads_per_site=5), threading.Event()
        )
        assert len(ads) <= 5
        assert len(ads) > 0

    def test_max_pages_limit(self, config: Config, spec: dict) -> None:
        """При max_pages_per_site=2 progress_cb вызывается ровно 2 раза."""
        parser = get_parser("avito.ru", config)
        calls: list[int] = []
        parser.collect(
            spec,
            _fast_options(max_pages_per_site=2, max_ads_per_site=500),
            threading.Event(),
            progress_cb=lambda p, n: calls.append(p),
        )
        assert calls == [1, 2]


# ----------------------------------------------------------------------
# 5-6. stop_event (S-12)
# ----------------------------------------------------------------------
class TestStopEvent:
    """Прерывание сбора по stop_event."""

    def test_stop_before_collect(
        self, config: Config, spec: dict
    ) -> None:
        """Установленный до вызова stop_event -> пустой список без зависания."""
        parser = get_parser("auto.ru", config)
        stop = threading.Event()
        stop.set()
        start = time.monotonic()
        ads = parser.collect(spec, _fast_options(), stop)
        elapsed = time.monotonic() - start
        assert ads == []
        assert elapsed < 1.0, "collect не должен выполня работу при остановке"

    def test_stop_during_pause(self, config: Config, spec: dict) -> None:
        """stop_event во время паузы -> быстрый выход (< 2 с) с частичным результатом."""
        parser = get_parser("bibika.ru", config)
        stop = threading.Event()
        result: list[Ad] = []

        def run() -> None:
            result.extend(
                parser.collect(
                    spec,
                    _fast_options(test_fast_mode=False, max_pages_per_site=10),
                    stop,
                )
            )

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)  # даём собрать первую страницу и уйти в паузу
        stop.set()
        thread.join(timeout=2.0)

        assert not thread.is_alive(), "collect завис после stop_event"
        assert 0 < len(result) < 500, "ожидались частичные результаты"


# ----------------------------------------------------------------------
# 7-8. Симуляция CAPTCHA / блокировки
# ----------------------------------------------------------------------
class TestSimulatedErrors:
    """Флаги simulate_captcha / simulate_block для тестов FSM (3.4)."""

    def test_captcha_error(self, config: Config, spec: dict) -> None:
        """simulate_captcha=auto.ru -> CaptchaDetectedError с site/url."""
        parser = get_parser("auto.ru", config)
        with pytest.raises(CaptchaDetectedError) as excinfo:
            parser.collect(spec, _fast_options(simulate_captcha="auto.ru"), threading.Event())
        assert excinfo.value.site == "auto.ru"
        assert excinfo.value.url

    def test_captcha_other_site_not_affected(
        self, config: Config, spec: dict
    ) -> None:
        """Капча «назначена» drom.ru — auto.ru собирает штатно."""
        parser = get_parser("auto.ru", config)
        ads = parser.collect(
            spec, _fast_options(simulate_captcha="drom.ru", max_pages_per_site=1),
            threading.Event(),
        )
        assert ads

    def test_blocked_error(self, config: Config, spec: dict) -> None:
        """simulate_block=avito.ru -> BlockedError."""
        parser = get_parser("avito.ru", config)
        with pytest.raises(BlockedError) as excinfo:
            parser.collect(spec, _fast_options(simulate_block="avito.ru"), threading.Event())
        assert excinfo.value.site == "avito.ru"

    def test_errors_are_parser_error(self, config: Config, spec: dict) -> None:
        """Все ошибки — наследники ParserError (единый перехват в 3.4)."""
        for exc_cls in (CaptchaDetectedError, BlockedError, UnsupportedSiteError):
            assert issubclass(exc_cls, ParserError)


# ----------------------------------------------------------------------
# 9. Ad.to_dict / from_dict + связка со StateStore
# ----------------------------------------------------------------------
class TestAdSerialization:
    """Round-trip Ad <-> dict и совместимость с StateStore.save_ads."""

    def test_to_dict_contains_all_fields(self) -> None:
        """to_dict включает все поля схемы collected_ads; raw_json — dict."""
        ad = Ad(
            url="https://auto.ru/cars/1",
            price=1_500_000,
            year=2019,
            manufacturer="BMW",
            model="X5",
            transmission="АКПП",
            fuel="Бензин",
            engine_volume=3.0,
            mileage=60_000,
            raw_json={"k": "значение"},
        )
        data = ad.to_dict()
        expected = {
            "url", "price", "year", "manufacturer", "model", "transmission",
            "fuel", "engine_volume", "mileage", "raw_json",
        }
        assert set(data) == expected
        assert data["raw_json"] == {"k": "значение"}

    def test_from_dict_round_trip_and_extra_keys(self) -> None:
        """from_dict игнорирует лишние ключи; пустой url -> ValueError."""
        ad = Ad(url="https://x/y", price=100)
        restored = Ad.from_dict({**ad.to_dict(), "unexpected": 1})
        assert restored == ad
        with pytest.raises(ValueError):
            Ad.from_dict({"price": 1})

    def test_from_dict_loads_raw_json_string(self) -> None:
        """Связка с StateStore: raw_json из БД приходит строкой — from_dict парсит её."""
        ad = Ad(
            url="https://auto.ru/cars/1",
            price=100,
            raw_json={"source": "stub", "site": "auto.ru"},
        )
        stored = dict(ad.to_dict())
        stored["raw_json"] = json.dumps(stored["raw_json"], ensure_ascii=False)
        restored = Ad.from_dict(stored)
        assert restored.raw_json == ad.raw_json
        assert restored == ad

    def test_state_store_integration(self, config: Config, spec: dict, tmp_path) -> None:
        """Связка collect -> save_ads -> load_ads работает (raw_json сериализуется)."""
        from server.state import StateStore

        parser = get_parser("auto.ru", config)
        ads = parser.collect(
            spec, _fast_options(max_pages_per_site=1), threading.Event()
        )
        store = StateStore(db_path=tmp_path / "state.db")
        try:
            store.save_task("t-33", spec, {}, state="running")
            store.save_ads("t-33", "auto.ru", [a.to_dict() for a in ads])
            assert store.count_ads("t-33") == len(ads)
            loaded = store.load_ads("t-33", "auto.ru")
            assert loaded[0]["url"] == ads[0].url
            # round-trip обратно в Ad (load_ads отдаёт raw_json словарём)
            back = Ad.from_dict(loaded[0])
            assert back.price == ads[0].price
            assert back.raw_json == ads[0].raw_json
        finally:
            store.close()


# ----------------------------------------------------------------------
# 10. test_fast_mode — время
# ----------------------------------------------------------------------
class TestFastMode:
    """Паузы в fast-режиме сокращены."""

    def test_fast_mode_is_quick(self, config: Config, spec: dict) -> None:
        """Полный прогон 4 страниц в fast-режиме занимает < 3 с."""
        parser = get_parser("drom.ru", config)
        start = time.monotonic()
        parser.collect(
            spec,
            _fast_options(max_pages_per_site=4, max_ads_per_site=500),
            threading.Event(),
        )
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"fast_mode слишком медленный: {elapsed:.2f} с"

    def test_normal_mode_delays(self, config: Config, spec: dict) -> None:
        """Без fast_mode паузы ~1–3 с: две страницы дольше 1 с суммарно."""
        parser = get_parser("bibika.ru", config)
        stop = threading.Event()
        start = time.monotonic()
        ads = parser.collect(
            spec,
            {"max_pages_per_site": 3, "max_ads_per_site": 500},
            stop,
        )
        elapsed = time.monotonic() - start
        # прервём, если вдруг затянулось — защита от «зависшего» теста
        stop.set()
        assert ads
        assert elapsed >= 1.0, "обычный режим не должен быть быстрее fast"
