"""Чтение данных из SQLite для Client (подзадача 4.1): справочники и объявления.

Клиент **не пишет** в базы и не создаёт схему: оба файла открываются
только для чтения (``mode=ro``). Это безопасно, пока Server и Client работают
на одной машине (так решено в ТЗ).

* :func:`load_reference` — справочник ``data/aprice.db`` (марки, модели,
  поколения, комплектации) целиком в памяти (база небольшая);
* :func:`load_ads` — объявления задания из ``collected_ads`` серверной базы
  ``data/aprice_server_state.db``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[Path, str]


class RepositoryError(Exception):
    """База недоступна, повреждена или имеет неожиданную схему."""


@dataclass(frozen=True)
class Generation:
    """Поколение модели; ``year_to`` ``None`` — выпускается до сих пор."""

    name: str
    year_from: Optional[int]
    year_to: Optional[int]
    transmissions: tuple[str, ...] = ()
    fuels: tuple[str, ...] = ()


@dataclass
class ReferenceData:
    """Справочник: ``марка -> модель -> [поколения]`` (порядок — по алфавиту)."""

    _tree: dict[str, dict[str, list[Generation]]] = field(default_factory=dict)

    def manufacturers(self) -> list[str]:
        """Марки по алфавиту."""
        return sorted(self._tree, key=str.casefold)

    def models(self, manufacturer: str) -> list[str]:
        """Модели марки по алфавиту (неизвестная марка — пустой список)."""
        return sorted(self._tree.get(manufacturer, {}), key=str.casefold)

    def generations(self, manufacturer: str, model: str) -> list[Generation]:
        """Поколения модели (новые первыми)."""
        found = self._tree.get(manufacturer, {}).get(model, [])
        return sorted(found, key=lambda g: g.year_from or 0, reverse=True)

    def generation(self, manufacturer: str, model: str, name: str) -> Optional[Generation]:
        """Поколение по имени или ``None``."""
        for gen in self.generations(manufacturer, model):
            if gen.name == name:
                return gen
        return None

    def year_bounds(
        self, manufacturer: str, model: str, generation: str = ""
    ) -> Optional[tuple[int, Optional[int]]]:
        """Границы годов выпуска ``(с, по)`` для поколения или всей модели.

        ``по`` — ``None``, если выпуск продолжается. ``None`` — в справочнике нет годов.
        """
        if generation:
            gens = [g for g in [self.generation(manufacturer, model, generation)] if g]
        else:
            gens = self.generations(manufacturer, model)
        gens = [g for g in gens if g.year_from is not None]
        if not gens:
            return None
        start = min(g.year_from for g in gens)  # type: ignore[type-var]
        if any(g.year_to is None for g in gens):
            return start, None
        return start, max(g.year_to for g in gens)  # type: ignore[type-var]

    def transmissions(self, manufacturer: str, model: str, generation: str = "") -> list[str]:
        """Варианты КПП из комплектаций (по поколению или по всей модели)."""
        return self._distinct(manufacturer, model, generation, "transmissions")

    def fuels(self, manufacturer: str, model: str, generation: str = "") -> list[str]:
        """Варианты топлива из комплектаций (по поколению или по всей модели)."""
        return self._distinct(manufacturer, model, generation, "fuels")

    def _distinct(self, manufacturer: str, model: str, generation: str, attr: str) -> list[str]:
        if generation:
            gens = [g for g in [self.generation(manufacturer, model, generation)] if g]
        else:
            gens = self.generations(manufacturer, model)
        values: set[str] = set()
        for gen in gens:
            values.update(getattr(gen, attr))
        return sorted(values, key=str.casefold)  # порядок не зависит от выдачи SQLite


def _connect_readonly(path: PathLike) -> sqlite3.Connection:
    """Открывает SQLite-файл только для чтения.

    Raises:
        RepositoryError: файла нет или он не открывается.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise RepositoryError(f"файл базы данных не найден: {db_path}")
    try:
        return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise RepositoryError(f"не удалось открыть {db_path}: {exc}") from exc


def load_reference(db_path: PathLike) -> ReferenceData:
    """Загружает справочник из ``aprice.db``.

    Args:
        db_path: путь к ``data/aprice.db``.

    Raises:
        RepositoryError: база недоступна или в ней нет ожидаемых таблиц.
    """
    conn = _connect_readonly(db_path)
    try:
        configs: dict[int, tuple[list[str], list[str]]] = {}
        for gen_id, transmission, fuel in conn.execute(
            "SELECT generation_id, transmission, fuel_type FROM configurations"
        ):
            trans, fuels = configs.setdefault(gen_id, ([], []))
            if transmission and transmission not in trans:
                trans.append(transmission)
            if fuel and fuel not in fuels:
                fuels.append(fuel)

        tree: dict[str, dict[str, list[Generation]]] = {}
        rows = conn.execute(
            "SELECT ma.name, mo.name, g.id, g.name, g.year_from, g.year_to"
            " FROM manufacturers ma"
            " JOIN models mo ON mo.manufacturer_id = ma.id"
            " LEFT JOIN generations g ON g.model_id = mo.id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RepositoryError(f"не удалось прочитать справочник: {exc}") from exc
    finally:
        conn.close()

    for maker, model, gen_id, gen_name, year_from, year_to in rows:
        models = tree.setdefault(maker, {})
        generations = models.setdefault(model, [])
        if gen_id is not None:
            trans, fuels = configs.get(gen_id, ([], []))
            generations.append(
                Generation(gen_name, year_from, year_to, tuple(trans), tuple(fuels))
            )
    return ReferenceData(tree)


def load_ads(db_path: PathLike, task_id: str) -> list[dict[str, Any]]:
    """Читает объявления задания из ``collected_ads`` серверной базы.

    Args:
        db_path: путь к ``data/aprice_server_state.db``.
        task_id: идентификатор задания.

    Returns:
        list[dict]: строки с ключами ``site, url, price, year, manufacturer, model,
        transmission, fuel, engine_volume, mileage``.

    Raises:
        RepositoryError: база недоступна или нет таблицы ``collected_ads``.
    """
    conn = _connect_readonly(db_path)
    try:
        cursor = conn.execute(
            "SELECT site, url, price, year, manufacturer, model, transmission,"
            " fuel, engine_volume, mileage FROM collected_ads"
            " WHERE task_id = ? ORDER BY site, url",
            (task_id,),
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        raise RepositoryError(f"не удалось прочитать объявления: {exc}") from exc
    finally:
        conn.close()


def count_by_site(ads: list[dict[str, Any]]) -> dict[str, int]:
    """Число объявлений по площадкам (в порядке первого появления)."""
    result: dict[str, int] = {}
    for ad in ads:
        result[ad["site"]] = result.get(ad["site"], 0) + 1
    return result
