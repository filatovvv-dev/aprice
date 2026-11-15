"""Тесты client.history (4.2): сохранение, лимит, порядок, устойчивость к порче."""

import json
from datetime import datetime, timedelta

import pytest

from client import history
from client.history import describe, load_history, save_to_history
from client.stats import calculate_stats

SPEC = {"mode": "exact", "manufacturer": "BMW", "model": "X5",
        "year_from": 2018, "year_to": 2021, "transmissions": [], "fuels": []}
T0 = datetime(2026, 11, 15, 10, 30, 0)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "history.json"


def stats_for(n=20):
    return calculate_stats([1_000_000 + i * 10_000 for i in range(n)])


def test_save_and_load_roundtrip(path):
    stats = stats_for()
    entry = save_to_history(stats, SPEC, path=path, now=T0)
    loaded = load_history(path)
    assert loaded == [entry]
    assert entry["timestamp"] == "2026-11-15T10:30:00"
    assert entry["spec"] == SPEC
    for key in ("count", "min", "max", "mean", "median", "trimmed_mean", "warning"):
        assert entry[key] == stats[key]


def test_file_is_utf8_json_list_without_ads(path):
    save_to_history(stats_for(), {**SPEC, "manufacturer": "Лада"}, path=path, now=T0)
    raw = path.read_text(encoding="utf-8")
    assert "Лада" in raw  # не \u-экранирование
    data = json.loads(raw)
    assert isinstance(data, list) and set(data[0]) == {
        "timestamp", "spec", "count", "min", "max", "mean", "median", "trimmed_mean", "warning"}
    assert not path.with_name(path.name + ".tmp").exists()


def test_keeps_only_last_ten(path):
    for i in range(13):
        save_to_history(stats_for(10 + i), SPEC, path=path, now=T0 + timedelta(minutes=i))
    loaded = load_history(path)
    assert len(loaded) == history.MAX_ENTRIES == 10
    counts = [e["count"] for e in loaded]
    assert counts == list(range(22, 12, -1))  # самые старые 3 вытеснены
    assert 10 not in counts


def test_sorted_newest_first_regardless_of_insert_order(path):
    save_to_history(stats_for(10), SPEC, path=path, now=T0 + timedelta(hours=2))
    save_to_history(stats_for(11), SPEC, path=path, now=T0)
    save_to_history(stats_for(12), SPEC, path=path, now=T0 + timedelta(hours=1))
    assert [e["count"] for e in load_history(path)] == [10, 12, 11]


def test_same_timestamp_later_added_first(path):
    save_to_history(stats_for(10), SPEC, path=path, now=T0)
    save_to_history(stats_for(11), SPEC, path=path, now=T0)
    assert [e["count"] for e in load_history(path)] == [11, 10]


def test_corrupted_json_returns_empty_list(path):
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_history(path) == []


def test_wrong_structure_returns_empty_or_filtered(path):
    path.parent.mkdir(parents=True)
    path.write_text('{"a": 1}', encoding="utf-8")
    assert load_history(path) == []
    path.write_text('[1, "x", {"timestamp": "2026-01-01T00:00:00", "count": 1}]', encoding="utf-8")
    assert [e["count"] for e in load_history(path)] == [1]


def test_missing_file_returns_empty_list(path):
    assert load_history(path) == []


def test_save_over_corrupted_file_starts_fresh(path):
    path.parent.mkdir(parents=True)
    path.write_text("\x00garbage", encoding="utf-8")
    save_to_history(stats_for(), SPEC, path=path, now=T0)
    assert len(load_history(path)) == 1


def test_saved_spec_is_a_copy(path):
    spec = dict(SPEC)
    entry = save_to_history(stats_for(), spec, path=path, now=T0)
    spec["model"] = "changed"
    assert entry["spec"]["model"] == "X5"


def test_default_path_comes_from_constants(tmp_path, monkeypatch):
    target = tmp_path / "h.json"
    monkeypatch.setattr(history.constants, "CLIENT_HISTORY_FILE", target)
    save_to_history(stats_for(), SPEC, now=T0)
    assert target.exists() and len(load_history()) == 1


def test_describe_entry():
    entry = {"timestamp": "2026-11-15T10:30:00", "spec": SPEC, "count": 564}
    assert describe(entry) == "15.11.2026 10:30 — BMW X5, 2018–2021 (564 объявл.)"
    exact = {**entry, "spec": {**SPEC, "year_from": 2019, "year_to": 2019}}
    assert "BMW X5, 2019 (" in describe(exact)


def test_describe_tolerates_broken_entry():
    assert describe({}) == "— — — (0 объявл.)"
    assert describe({"timestamp": "bad", "spec": "x", "count": 3}).startswith("— — — (3")
