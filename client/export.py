"""Экспорт объявлений в CSV / XLSX (подзадача 4.4). Модуль без Qt.

В файл попадают **сырые объявления** (не только итоговые цифры), столбцы:
``Площадка, Марка, Модель, Год, Цена, Пробег, Ссылка``.

* CSV: разделитель ``;`` и кодировка ``utf-8-sig`` (BOM), чтобы Excel сразу открывал
  кириллицу и раскладывал столбцы.
* XLSX: ``pandas`` + ``openpyxl``.
* Текстовые ячейки, начинающиеся с ``= + - @``, экранируются апострофом: данные с
  площадок не должны исполняться как формулы Excel.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Union

import pandas as pd

#: Заголовки столбцов и ключи объявления (``collected_ads``), из которых они берутся.
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Площадка", "site"),
    ("Марка", "manufacturer"),
    ("Модель", "model"),
    ("Год", "year"),
    ("Цена", "price"),
    ("Пробег", "mileage"),
    ("Ссылка", "url"),
)
HEADERS = tuple(title for title, _ in EXPORT_COLUMNS)
CSV_DELIMITER = ";"
SUPPORTED_SUFFIXES = (".csv", ".xlsx")
_FORMULA_PREFIXES = ("=", "+", "-", "@")

PathLike = Union[Path, str]


class ExportError(Exception):
    """Экспорт невозможен (неизвестный формат, файл не записан)."""


def _safe(value: Any) -> Any:
    """``None`` -> пустая ячейка; строки-«формулы» экранируются."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def build_rows(ads: Iterable[dict]) -> list[list[Any]]:
    """Строки таблицы экспорта (без заголовка) в порядке :data:`EXPORT_COLUMNS`."""
    return [[_safe(ad.get(key)) for _, key in EXPORT_COLUMNS] for ad in ads]


def export_ads(file_path: PathLike, ads: Iterable[dict]) -> int:
    """Записывает объявления в ``.csv`` или ``.xlsx`` (по расширению).

    Returns:
        int: число записанных объявлений.

    Raises:
        ExportError: неподдерживаемое расширение или файл не удалось записать
            (например, он открыт в Excel).
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ExportError(
            f"Неподдерживаемый формат «{path.suffix}»: выберите .csv или .xlsx"
        )
    rows = build_rows(ads)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".csv":
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh, delimiter=CSV_DELIMITER)
                writer.writerow(HEADERS)
                writer.writerows(rows)
        else:
            frame = pd.DataFrame(rows, columns=list(HEADERS))
            frame.to_excel(path, index=False, engine="openpyxl", sheet_name="Объявления")
    except OSError as exc:
        raise ExportError(f"Не удалось записать файл: {exc}") from exc
    return len(rows)
