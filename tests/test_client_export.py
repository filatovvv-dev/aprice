"""Тесты client.export (4.4): CSV и XLSX с заголовками и сырыми объявлениями."""

import csv

import pandas as pd
import pytest

from client.export import HEADERS, ExportError, build_rows, export_ads

ADS = [
    {"site": "auto.ru", "manufacturer": "BMW", "model": "X5", "year": 2019, "price": 5_640_000,
     "mileage": 45_000, "url": "https://auto.ru/cars/1", "engine_volume": 3.0},
    {"site": "drom.ru", "manufacturer": "BMW", "model": "X5", "year": 2020, "price": 6_100_000,
     "mileage": None, "url": "https://drom.ru/2"},
]
EXPECTED_HEADERS = ["Площадка", "Марка", "Модель", "Год", "Цена", "Пробег", "Ссылка"]


def test_headers_order():
    assert list(HEADERS) == EXPECTED_HEADERS


def test_export_csv(tmp_path):
    path = tmp_path / "out.csv"
    assert export_ads(path, ADS) == 2
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM: Excel увидит UTF-8
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert rows[0] == EXPECTED_HEADERS
    assert rows[1] == ["auto.ru", "BMW", "X5", "2019", "5640000", "45000", "https://auto.ru/cars/1"]
    assert rows[2][5] == ""  # пробег неизвестен — пустая ячейка
    assert len(rows) == 3  # лишние поля объявления (engine_volume) не попали


def test_export_xlsx(tmp_path):
    path = tmp_path / "out.xlsx"
    assert export_ads(path, ADS) == 2
    frame = pd.read_excel(path, engine="openpyxl")
    assert list(frame.columns) == EXPECTED_HEADERS
    assert len(frame) == 2
    assert frame.loc[0, "Цена"] == 5_640_000 and frame.loc[1, "Год"] == 2020
    assert frame.loc[0, "Ссылка"] == "https://auto.ru/cars/1"


def test_empty_export_has_only_headers(tmp_path):
    csv_path, xlsx_path = tmp_path / "e.csv", tmp_path / "e.xlsx"
    assert export_ads(csv_path, []) == 0 and export_ads(xlsx_path, []) == 0
    assert csv_path.read_text(encoding="utf-8-sig").strip() == ";".join(EXPECTED_HEADERS)
    assert list(pd.read_excel(xlsx_path, engine="openpyxl").columns) == EXPECTED_HEADERS


def test_extension_is_case_insensitive(tmp_path):
    assert export_ads(tmp_path / "OUT.CSV", ADS) == 2
    assert export_ads(tmp_path / "OUT.XLSX", ADS) == 2


def test_unknown_extension_rejected(tmp_path):
    with pytest.raises(ExportError, match="формат"):
        export_ads(tmp_path / "out.txt", ADS)
    with pytest.raises(ExportError):
        export_ads(tmp_path / "noext", ADS)
    assert list(tmp_path.iterdir()) == []


def test_creates_missing_directory(tmp_path):
    assert export_ads(tmp_path / "new" / "dir" / "o.csv", ADS) == 2


def test_unwritable_target_raises_export_error(tmp_path):
    (tmp_path / "dir.csv").mkdir()  # путь занят каталогом
    with pytest.raises(ExportError, match="Не удалось записать"):
        export_ads(tmp_path / "dir.csv", ADS)


def test_formula_like_text_is_escaped(tmp_path):
    ads = [{"site": "=HYPERLINK(1)", "manufacturer": "+A", "model": "-B", "year": 2019,
            "price": 1, "mileage": 2, "url": "@x"}]
    assert build_rows(ads)[0][:3] == ["'=HYPERLINK(1)", "'+A", "'-B"]
    path = tmp_path / "f.csv"
    export_ads(path, ads)
    assert path.read_text(encoding="utf-8-sig").splitlines()[1].startswith("'=HYPERLINK(1);'+A;'-B;")
    assert build_rows(ads)[0][6] == "'@x"
    assert build_rows([{"price": -5}])[0][4] == -5  # числа не трогаем


def test_accepts_generator(tmp_path):
    assert export_ads(tmp_path / "g.csv", (a for a in ADS)) == 2
