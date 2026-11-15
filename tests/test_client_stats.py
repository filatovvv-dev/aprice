"""Тесты client.stats (4.2): расчёт показателей и предупреждения."""

import math

import pytest

from client.stats import (
    WARNING_FEW,
    WARNING_NO_DATA,
    calculate_stats,
)


def test_empty_list():
    stats = calculate_stats([])
    assert stats["count"] == 0
    for key in ("min", "max", "mean", "median", "trimmed_mean"):
        assert stats[key] is None
    assert stats["warning"] == WARNING_NO_DATA


def test_single_item():
    stats = calculate_stats([5_000_000])
    assert stats["count"] == 1
    assert stats["min"] == stats["max"] == 5_000_000
    assert stats["mean"] == stats["median"] == stats["trimmed_mean"] == 5_000_000.0
    assert stats["warning"] == WARNING_FEW


def test_five_items():
    stats = calculate_stats([100, 200, 300, 400, 1000])
    assert stats["count"] == 5
    assert (stats["min"], stats["max"]) == (100, 1000)
    assert stats["mean"] == 400.0
    assert stats["median"] == 300.0
    assert stats["trimmed_mean"] == 400.0  # k = 0: отбрасывать нечего
    assert stats["warning"] == WARNING_FEW


def test_twenty_items_trimmed_mean():
    # k = 2: отбрасываются две самые дешёвые (100) и две самые дорогие (1000).
    # Оставшиеся 8×100 + 8×1000 -> 550. (В ТЗ ожидалось 100 — это ошибка в примере.)
    stats = calculate_stats([100] * 10 + [1000] * 10)
    assert stats["trimmed_mean"] == 550.0
    assert stats["mean"] == 550.0
    assert stats["warning"] is None


def test_twenty_items_outliers_trimmed_to_100():
    stats = calculate_stats([100] * 18 + [1000] * 2)
    assert stats["trimmed_mean"] == 100.0
    assert stats["mean"] == 190.0


def test_eleven_items_trimmed_mean():
    stats = calculate_stats(list(range(1, 11)) + [1000])
    assert stats["count"] == 11
    assert stats["trimmed_mean"] == 6.0  # k = 1: отброшены 1 и 1000
    assert stats["median"] == 6.0
    assert stats["warning"] is None


def test_exactly_ten_items_no_warning_and_k_is_one():
    stats = calculate_stats(list(range(1, 11)))
    assert stats["warning"] is None
    assert stats["trimmed_mean"] == 5.5  # отброшены 1 и 10


def test_nine_items_warns():
    assert calculate_stats([1] * 9)["warning"] == WARNING_FEW


def test_real_case_unsorted_prices():
    prices = [5_640_000, 6_100_000, 4_900_000, 7_250_000, 5_980_000, 6_500_000,
              5_300_000, 9_050_000, 4_200_000, 6_800_000, 5_750_000, 6_050_000]
    stats = calculate_stats(prices)
    assert stats["count"] == 12
    assert stats["min"] == 4_200_000 and stats["max"] == 9_050_000
    assert stats["median"] == pytest.approx(6_015_000.0)
    # k = 1: убраны 4 200 000 и 9 050 000
    expected = (sum(prices) - 4_200_000 - 9_050_000) / 10
    assert stats["trimmed_mean"] == pytest.approx(expected)
    assert stats["trimmed_mean"] < stats["mean"]  # дорогой выброс тянет среднее вверх


def test_negative_zero_and_garbage_prices_are_ignored():
    stats = calculate_stats([-5, 0, None, "abc", True, float("nan"), float("inf"), 100, 200])
    assert stats["count"] == 2
    assert stats["ignored"] == 7
    assert stats["min"] == 100 and stats["max"] == 200
    assert "Исключено некорректных цен: 7." in stats["warning"]
    assert WARNING_FEW in stats["warning"]


def test_only_invalid_prices_means_no_data():
    stats = calculate_stats([-1, -2, 0])
    assert stats["count"] == 0
    assert stats["min"] is None
    assert stats["warning"].startswith(WARNING_NO_DATA)
    assert stats["ignored"] == 3


def test_types_and_input_not_mutated():
    prices = [300, 100, 200]
    stats = calculate_stats(prices)
    assert prices == [300, 100, 200]
    assert isinstance(stats["min"], int)
    assert type(stats["mean"]) is float and type(stats["median"]) is float
    assert type(stats["trimmed_mean"]) is float
    assert not math.isnan(stats["mean"])


def test_accepts_generator_and_floats():
    stats = calculate_stats(p for p in (1.5, 2.5, 3.5))
    assert stats["count"] == 3 and stats["mean"] == 2.5


def test_large_list_has_no_warning():
    stats = calculate_stats(list(range(1000, 2000)))
    assert stats["count"] == 1000 and stats["warning"] is None
    assert stats["trimmed_mean"] == pytest.approx(1499.5)
