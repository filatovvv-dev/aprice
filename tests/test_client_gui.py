"""Тесты APrice-Client 4.1: форма, окно, контроллер, чтение БД.

Сети и настоящего Server нет: IPC подменён ``FakeIpc``, базы — временные
SQLite-файлы (серверная создаётся настоящим ``StateStore``). Окно — настоящий
``MainWindow``; модальный ``QMessageBox.critical`` подменён (иначе тест завис бы).
Общий ``QApplication`` — фикстура ``qapp`` из ``conftest.py``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

import pytest
from PyQt6.QtWidgets import QApplication

from common.config import Config
from common.ipc_client import IpcConnectionError, IpcTimeoutError
from client import controller as controller_module
from client import form
from client import gui as gui_module
from client.controller import (
    MAX_POLL_FAILURES,
    POLL_INTERVAL_MS,
    SERVER_DOWN_MESSAGE,
    ClientController,
)
from client.form import FormState
from client.gui import ANY_TEXT, NO_VALUE, PLACEHOLDER, STAT_ROWS, MainWindow
from client.repository import (
    RepositoryError,
    count_by_site,
    load_ads,
    load_reference,
)
from server.state import StateStore

_REFERENCE_SQL = """
CREATE TABLE manufacturers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    source_id TEXT, updated_at TEXT NOT NULL);
CREATE TABLE models (id INTEGER PRIMARY KEY AUTOINCREMENT, manufacturer_id INTEGER NOT NULL,
    name TEXT NOT NULL, source_id TEXT, updated_at TEXT NOT NULL, UNIQUE (manufacturer_id, name));
CREATE TABLE generations (id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL,
    name TEXT NOT NULL, year_from INTEGER, year_to INTEGER, source_id TEXT,
    updated_at TEXT NOT NULL, UNIQUE (model_id, name));
CREATE TABLE configurations (id INTEGER PRIMARY KEY AUTOINCREMENT, generation_id INTEGER NOT NULL,
    transmission TEXT, fuel_type TEXT, engine_volume_from REAL, engine_volume_to REAL,
    power_from INTEGER, power_to INTEGER, updated_at TEXT NOT NULL,
    UNIQUE (generation_id, transmission, fuel_type));
INSERT INTO manufacturers (id, name, updated_at) VALUES (1, 'BMW', 't'), (2, 'Audi', 't');
INSERT INTO models (id, manufacturer_id, name, updated_at) VALUES
    (1, 1, 'X5', 't'), (2, 1, '3 серии', 't'), (3, 2, 'A4', 't'), (4, 2, 'A6', 't');
INSERT INTO generations (id, model_id, name, year_from, year_to, updated_at) VALUES
    (1, 1, 'III (F15)', 2013, 2018, 't'), (2, 1, 'IV (G05)', 2018, NULL, 't'),
    (3, 3, 'B9', 2015, 2019, 't');
INSERT INTO configurations (generation_id, transmission, fuel_type, updated_at) VALUES
    (1, 'АКПП', 'Дизель', 't'), (1, 'АКПП', 'Бензин', 't'), (2, 'АКПП', 'Бензин', 't'),
    (3, 'МКПП', NULL, 't');
"""


# ----------------------------------------------------------------------
# Фикстуры и заглушки
# ----------------------------------------------------------------------
class FakeIpc:
    """Подмена ``IpcClient``: записывает вызовы, ответы заданы сценарием."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.submit_reply: object = {"type": "task_accepted", "task_id": "T1",
                                     "estimated_seconds": 60, "queue_position": 0}
        self.status_replies: list[object] = []
        self.stop_reply: object = {"type": "status", "task_id": "T1", "state": "stopping"}

    @staticmethod
    def _answer(reply):
        if isinstance(reply, Exception):
            raise reply
        return reply

    def submit_task(self, spec, options):
        self.calls.append(("submit_task", spec, options))
        return self._answer(self.submit_reply)

    def get_status(self, task_id):
        self.calls.append(("get_status", task_id))
        reply = self.status_replies.pop(0) if len(self.status_replies) > 1 else self.status_replies[0]
        return self._answer(reply)

    def stop_task(self, task_id):
        self.calls.append(("stop_task", task_id))
        return self._answer(self.stop_reply)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class Dialogs:
    """Собирает тексты ``QMessageBox.critical`` вместо показа окна."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def critical(self, parent, title, text, *args):
        self.messages.append(text)


@pytest.fixture(autouse=True)
def dialogs(monkeypatch) -> Dialogs:
    recorded = Dialogs()
    monkeypatch.setattr(gui_module, "QMessageBox", recorded)
    return recorded


@pytest.fixture()
def reference_db(tmp_path) -> Path:
    path = tmp_path / "aprice.db"
    conn = sqlite3.connect(path)
    conn.executescript(_REFERENCE_SQL)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def state_db(tmp_path) -> Path:
    """Серверная база с задачей ``T1``: 2 объявления auto.ru и 1 drom.ru."""
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.save_task("T1", {"mode": "exact"}, {}, "completed")
    store.save_ads("T1", "auto.ru", [
        {"url": "https://auto.ru/1", "price": 5_000_000, "year": 2020},
        {"url": "https://auto.ru/2", "price": 5_500_000, "year": 2020},
    ])
    store.save_ads("T1", "drom.ru", [{"url": "https://drom.ru/1.html", "price": 4_900_000}])
    store.save_task("T2", {"mode": "exact"}, {}, "completed")
    store.save_ads("T2", "avito.ru", [{"url": "https://avito.ru/9", "price": 1}])
    store.close()
    return path


@pytest.fixture()
def window(qapp) -> MainWindow:
    return MainWindow()


@pytest.fixture()
def ipc() -> FakeIpc:
    return FakeIpc()


@pytest.fixture()
def ctrl(window, ipc, reference_db, state_db, tmp_path):
    controller = ClientController(
        window, Config(tmp_path / "settings.json"), ipc=ipc,
        reference_db=reference_db, state_db=state_db,
    )
    yield controller
    controller.shutdown()


def choose(combo, text: str) -> None:
    """Выбирает пункт списка так, как это делает пользователь (с сигналом)."""
    for index in range(combo.count()):
        if combo.itemText(index) == text:
            combo.setCurrentIndex(index)
            return
    raise AssertionError(f"нет пункта {text!r}: {[combo.itemText(i) for i in range(combo.count())]}")


def items(combo) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def pick_bmw_x5(window, year: Optional[str] = None) -> None:
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    if year:
        choose(window.year_combo, year)


def pump_until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.005)
    assert cond(), "условие не выполнено за отведённое время"


# ----------------------------------------------------------------------
# Форма (без Qt)
# ----------------------------------------------------------------------
def test_form_exact_spec_matches_server_contract():
    spec = form.build_spec(FormState(
        mode="exact", manufacturer="BMW", model="X5", generation="IV (G05)", year=2020,
        transmission="АКПП", fuel="Бензин",
    ))
    assert spec == {
        "mode": "exact", "manufacturer": "BMW", "model": "X5", "year_from": 2020,
        "year_to": 2020, "generation": "IV (G05)", "transmissions": ["АКПП"],
        "fuels": ["Бензин"],
    }
    from server import protocol
    protocol.validate_spec(spec)  # контракт Server принимает такой spec


def test_form_exact_without_optional_fields():
    spec = form.build_spec(FormState(mode="exact", manufacturer="BMW", model="X5", year=2019))
    assert "generation" not in spec and spec["transmissions"] == [] and spec["fuels"] == []


def test_form_range_spec_matches_server_contract():
    spec = form.build_spec(FormState(
        mode="range", manufacturer="BMW", model="X5", year_from=2018, year_to=2021,
        volume_from=2.0, volume_to=3.0, transmissions=["АКПП", "Робот"], fuels=["Дизель"],
    ))
    assert spec == {
        "mode": "range", "manufacturer": "BMW", "model": "X5", "year_from": 2018,
        "year_to": 2021, "transmissions": ["АКПП", "Робот"], "fuels": ["Дизель"],
        "engine_volume_from": 2.0, "engine_volume_to": 3.0,
    }
    from server import protocol
    protocol.validate_spec(spec)


def test_form_range_without_volume_omits_keys():
    spec = form.build_spec(FormState(mode="range", manufacturer="BMW", model="X5",
                                     year_from=2018, year_to=2018))
    assert "engine_volume_from" not in spec and "engine_volume_to" not in spec


@pytest.mark.parametrize("state, message", [
    (FormState(manufacturer="", model="X5", year=2020), "марку"),
    (FormState(manufacturer="BMW", model="  ", year=2020), "модель"),
    (FormState(manufacturer="BMW", model="X5", year=None), "год"),
    (FormState(mode="range", manufacturer="BMW", model="X5", year_from=2021, year_to=2018), "больше"),
    (FormState(mode="range", manufacturer="BMW", model="X5", year_from=2018, year_to=2021,
               volume_from=3.0, volume_to=2.0), "Объём"),
    (FormState(mode="weird", manufacturer="BMW", model="X5"), "режим"),
])
def test_form_errors(state, message):
    assert not form.is_ready(state)
    assert message in (form.form_error(state) or "")
    with pytest.raises(ValueError):
        form.build_spec(state)


def test_form_options_default_to_server_defaults():
    assert form.build_options() == {}


# ----------------------------------------------------------------------
# MainWindow
# ----------------------------------------------------------------------
def test_window_created_and_evaluate_initially_disabled(window):
    assert window.windowTitle() == gui_module.WINDOW_TITLE
    assert not window.evaluate_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert window.radio_exact.isChecked()


def test_switching_to_range_mode_swaps_forms(window):
    assert window.exact_box.isVisibleTo(window) and not window.range_box.isVisibleTo(window)
    window.radio_range.setChecked(True)
    assert window.range_box.isVisibleTo(window) and not window.exact_box.isVisibleTo(window)
    assert not window.radio_exact.isChecked()  # радиокнопки взаимоисключающие
    window.radio_exact.setChecked(True)
    assert window.exact_box.isVisibleTo(window) and not window.range_box.isVisibleTo(window)


def test_evaluate_requires_manufacturer_and_model(window):
    window.set_manufacturers(["BMW", "Audi"])
    window.set_years([2021, 2020])
    assert not window.evaluate_button.isEnabled()  # ничего не выбрано
    choose(window.manufacturer_combo, "BMW")
    assert not window.evaluate_button.isEnabled()  # нет модели
    window.set_models(["X5"])
    choose(window.model_combo, "X5")
    assert window.evaluate_button.isEnabled()
    window.model_combo.setCurrentIndex(0)  # снова «— выберите —»
    assert not window.evaluate_button.isEnabled()


def test_range_mode_needs_valid_year_range(window):
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    window.radio_range.setChecked(True)
    assert window.evaluate_button.isEnabled()
    window.year_from_spin.setValue(window.year_to_spin.value())
    window.year_to_spin.setValue(window.year_from_spin.value() - 5)
    assert not window.evaluate_button.isEnabled()  # «от» больше «до»


def test_form_state_reads_widgets(window):
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
    window.set_years([2021, 2020])
    window.set_transmissions(["АКПП"])
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    choose(window.transmission_combo, "АКПП")
    state = window.form_state()
    assert (state.mode, state.manufacturer, state.model) == ("exact", "BMW", "X5")
    assert (state.year, state.transmission, state.fuel, state.generation) == (2021, "АКПП", "", "")

    window.radio_range.setChecked(True)
    window.transmission_checks["Робот"].setChecked(True)
    window.fuel_checks["Дизель"].setChecked(True)
    window.volume_from_spin.setValue(2.5)
    state = window.form_state()
    assert state.mode == "range" and state.transmissions == ["Робот"] and state.fuels == ["Дизель"]
    assert state.volume_from == 2.5 and state.volume_to is None  # 0 = не задано


def test_programmatic_fill_does_not_emit_user_signals(window):
    seen: list[str] = []
    window.manufacturer_changed.connect(seen.append)
    window.set_manufacturers(["BMW", "Audi"])
    assert seen == []
    choose(window.manufacturer_combo, "Audi")
    assert seen == ["Audi"]


def test_combos_start_with_placeholder_and_return_empty_selection(window):
    window.set_manufacturers(["BMW"])
    assert window.manufacturer_combo.itemText(0) == PLACEHOLDER
    assert window.form_state().manufacturer == ""
    window.set_generations(["G1"])
    assert window.generation_combo.itemText(0) == ANY_TEXT and window.form_state().generation == ""


def test_buttons_follow_running_state_and_progress_is_indeterminate(window):
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
    window.set_years([2020])
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    assert window.evaluate_button.isEnabled() and not window.stop_button.isEnabled()
    window.set_running(True)
    assert not window.evaluate_button.isEnabled() and window.stop_button.isEnabled()
    assert (window.progress_bar.minimum(), window.progress_bar.maximum()) == (0, 0)
    window.set_running(False)
    assert window.evaluate_button.isEnabled() and not window.stop_button.isEnabled()
    assert window.progress_bar.maximum() == 100


def test_buttons_emit_requests(window):
    seen: list[str] = []
    window.evaluate_requested.connect(lambda: seen.append("evaluate"))
    window.stop_requested.connect(lambda: seen.append("stop"))
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
    window.set_years([2020])
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    window.evaluate_button.click()
    window.set_running(True)
    window.stop_button.click()
    assert seen == ["evaluate", "stop"]


def test_results_panel_shows_count_sites_and_placeholder_stats(window):
    assert window.stats_table.rowCount() == len(STAT_ROWS) == 5
    assert [window.stats_table.item(r, 1).text() for r in range(5)] == [NO_VALUE] * 5
    window.show_results(3, {"auto.ru": 2, "drom.ru": 1})
    assert window.count_label.text() == "Объявлений: 3"
    assert [(window.sites_table.item(r, 0).text(), window.sites_table.item(r, 1).text())
            for r in range(2)] == [("auto.ru", "2"), ("drom.ru", "1")]
    window.clear_results()
    assert window.sites_table.rowCount() == 0 and window.count_label.text() == "Объявлений: —"


def test_show_error_uses_message_box(window, dialogs):
    window.show_error("ошибка")
    assert dialogs.messages == ["ошибка"]


# ----------------------------------------------------------------------
# Репозиторий
# ----------------------------------------------------------------------
def test_reference_tree(reference_db):
    ref = load_reference(reference_db)
    assert ref.manufacturers() == ["Audi", "BMW"]
    assert ref.models("BMW") == ["3 серии", "X5"]
    assert ref.models("Nope") == []
    assert [g.name for g in ref.generations("BMW", "X5")] == ["IV (G05)", "III (F15)"]  # новые первыми
    assert ref.generations("BMW", "3 серии") == []  # модель без поколений загружается
    assert ref.year_bounds("BMW", "X5") == (2013, None)  # G05 выпускается
    assert ref.year_bounds("BMW", "X5", "III (F15)") == (2013, 2018)
    assert ref.year_bounds("BMW", "3 серии") is None
    assert ref.transmissions("BMW", "X5") == ["АКПП"]
    assert ref.fuels("BMW", "X5", "III (F15)") == ["Бензин", "Дизель"]  # по алфавиту
    assert ref.fuels("Audi", "A4") == []  # NULL в справочнике не попадает в список


def test_missing_databases_raise_and_are_not_created(tmp_path):
    missing = tmp_path / "none.db"
    with pytest.raises(RepositoryError):
        load_reference(missing)
    with pytest.raises(RepositoryError):
        load_ads(missing, "T1")
    assert not missing.exists()  # читатель ничего не создаёт


def test_wrong_schema_raises(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(RepositoryError):
        load_reference(path)
    with pytest.raises(RepositoryError):
        load_ads(path, "T1")


def test_load_ads_filters_by_task_and_is_read_only(state_db):
    ads = load_ads(state_db, "T1")
    assert [a["site"] for a in ads] == ["auto.ru", "auto.ru", "drom.ru"]
    assert ads[0]["price"] == 5_000_000 and ads[0]["year"] == 2020
    assert count_by_site(ads) == {"auto.ru": 2, "drom.ru": 1}
    assert load_ads(state_db, "unknown") == []
    from client import repository
    conn = repository._connect_readonly(state_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM collected_ads")
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Контроллер: справочники
# ----------------------------------------------------------------------
def test_reference_fills_comboboxes_and_cascades(window, ctrl):
    assert items(window.manufacturer_combo) == [PLACEHOLDER, "Audi", "BMW"]
    assert items(window.model_combo) == [PLACEHOLDER]  # пока марка не выбрана
    choose(window.manufacturer_combo, "BMW")
    assert items(window.model_combo) == [PLACEHOLDER, "3 серии", "X5"]
    choose(window.model_combo, "X5")
    assert items(window.generation_combo) == [ANY_TEXT, "IV (G05)", "III (F15)"]
    assert items(window.transmission_combo) == [ANY_TEXT, "АКПП"]
    assert items(window.fuel_combo) == [ANY_TEXT, "Бензин", "Дизель"]
    years = [int(y) for y in items(window.year_combo)]
    assert years[0] == form.max_year() and years[-1] == 2013  # до текущего: справочник неполон
    assert window.evaluate_button.isEnabled()  # год выбран автоматически (самый новый)


def test_generation_limits_years_and_options(window, ctrl):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    choose(window.generation_combo, "III (F15)")
    assert [int(y) for y in items(window.year_combo)] == [2018, 2017, 2016, 2015, 2014, 2013]
    assert items(window.fuel_combo) == [ANY_TEXT, "Бензин", "Дизель"]
    window.generation_combo.setCurrentIndex(0)  # «Любая» — годы снова до текущего
    assert int(window.year_combo.itemText(0)) == form.max_year()


def test_changing_manufacturer_resets_model(window, ctrl):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    choose(window.manufacturer_combo, "Audi")
    assert window.form_state().model == ""
    assert items(window.model_combo) == [PLACEHOLDER, "A4", "A6"]
    assert not window.evaluate_button.isEnabled()


def test_model_without_reference_data_falls_back_to_static_lists(window, ctrl):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "3 серии")
    assert items(window.transmission_combo) == [ANY_TEXT, *form.TRANSMISSIONS]
    assert items(window.fuel_combo) == [ANY_TEXT, *form.FUELS]
    assert int(window.year_combo.itemText(0)) == form.max_year()
    assert int(window.year_combo.itemText(window.year_combo.count() - 1)) == 1990


def test_missing_reference_db_shows_status_and_does_not_crash(window, ipc, state_db, tmp_path):
    controller = ClientController(window, Config(tmp_path / "s.json"), ipc=ipc,
                                  reference_db=tmp_path / "missing.db", state_db=state_db)
    assert "Справочник недоступен" in window.status_label.text()
    assert items(window.manufacturer_combo) == [PLACEHOLDER]
    assert not window.evaluate_button.isEnabled()
    controller.shutdown()


# ----------------------------------------------------------------------
# Контроллер: отправка и опрос
# ----------------------------------------------------------------------
def test_submit_sends_spec_and_options_and_starts_polling(window, ctrl, ipc):
    pick_bmw_x5(window, "2020")
    choose(window.transmission_combo, "АКПП")
    window.evaluate_button.click()
    assert ipc.calls == [("submit_task", {
        "mode": "exact", "manufacturer": "BMW", "model": "X5", "year_from": 2020,
        "year_to": 2020, "transmissions": ["АКПП"], "fuels": [],
    }, {})]
    assert ctrl.task_id == "T1"
    assert ctrl.timer.isActive() and ctrl.timer.interval() == POLL_INTERVAL_MS == 2000
    assert window.status_label.text() == "Задание принято"
    assert window.stop_button.isEnabled() and not window.evaluate_button.isEnabled()


def test_submit_range_mode_spec(window, ctrl, ipc):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    window.radio_range.setChecked(True)
    window.year_from_spin.setValue(2018)
    window.year_to_spin.setValue(2021)
    window.fuel_checks["Дизель"].setChecked(True)
    window.evaluate_button.click()
    _, spec, _ = ipc.calls[0]
    assert spec == {"mode": "range", "manufacturer": "BMW", "model": "X5", "year_from": 2018,
                    "year_to": 2021, "transmissions": [], "fuels": ["Дизель"]}


def test_timer_really_polls_status(window, ipc, reference_db, state_db, tmp_path):
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "running"}]
    controller = ClientController(window, Config(tmp_path / "s.json"), ipc=ipc, poll_interval_ms=10,
                                  reference_db=reference_db, state_db=state_db)
    try:
        pick_bmw_x5(window)
        window.evaluate_button.click()
        pump_until(lambda: ipc.names().count("get_status") >= 2)
    finally:
        controller.shutdown()


@pytest.mark.parametrize("error", [IpcConnectionError("refused"), IpcTimeoutError("timeout")])
def test_server_down_shows_message_and_resets_ui(window, ctrl, ipc, dialogs, error):
    ipc.submit_reply = error
    pick_bmw_x5(window)
    window.evaluate_button.click()
    assert dialogs.messages == [SERVER_DOWN_MESSAGE] and "Сервер не запущен" in dialogs.messages[0]
    assert ctrl.task_id is None and not ctrl.timer.isActive()
    assert window.evaluate_button.isEnabled() and not window.stop_button.isEnabled()


def test_server_error_reply_is_shown(window, ctrl, ipc, dialogs):
    ipc.submit_reply = {"type": "error", "code": "invalid_spec", "message": "invalid_spec: плохо"}
    pick_bmw_x5(window)
    window.evaluate_button.click()
    assert "invalid_spec: плохо" in dialogs.messages[0]
    assert ctrl.task_id is None and not ctrl.timer.isActive()


def test_queue_position_is_shown(window, ctrl, ipc):
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T1", "queue_position": 2}
    pick_bmw_x5(window)
    window.evaluate_button.click()
    assert "впереди заданий: 2" in window.status_label.text()


def test_poll_running_updates_status_text(window, ctrl, ipc):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "running",
                           "current_site": "drom.ru", "progress": {"site": "drom.ru", "page": 2,
                                                                    "ads_collected": 40},
                           "eta_seconds": 100}]
    ctrl.poll()
    text = window.status_label.text()
    assert "drom.ru" in text and "страница 2" in text and "объявлений 40" in text and "мин" in text
    assert (window.progress_bar.minimum(), window.progress_bar.maximum()) == (0, 0)
    assert ctrl.timer.isActive()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "queued"}]
    ctrl.poll()
    assert window.status_label.text() == "Задание в очереди…"


def test_poll_completed_reads_ads_from_db_and_shows_results(window, ctrl, ipc):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
    ctrl.poll()
    assert window.count_label.text() == "Объявлений: 3"
    assert [window.sites_table.item(r, 0).text() for r in range(2)] == ["auto.ru", "drom.ru"]
    assert [window.sites_table.item(r, 1).text() for r in range(2)] == ["2", "1"]
    assert not ctrl.timer.isActive() and ctrl.task_id is None
    assert window.evaluate_button.isEnabled() and not window.stop_button.isEnabled()
    assert "собрано объявлений — 3" in window.status_label.text()


def test_completed_task_reads_only_its_own_ads(window, ctrl, ipc):
    pick_bmw_x5(window)
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T2"}
    window.evaluate_button.click()
    ipc.status_replies = [{"type": "status", "task_id": "T2", "state": "completed"}]
    ctrl.poll()
    assert window.count_label.text() == "Объявлений: 1"


def test_results_read_failure_is_reported(window, ipc, reference_db, tmp_path, dialogs):
    controller = ClientController(window, Config(tmp_path / "s.json"), ipc=ipc,
                                  reference_db=reference_db, state_db=tmp_path / "no.db")
    try:
        pick_bmw_x5(window)
        window.evaluate_button.click()
        ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
        controller.poll()
        assert "не прочитаны" in window.status_label.text()
        assert "Не удалось прочитать результаты" in dialogs.messages[-1]
        assert window.evaluate_button.isEnabled()
    finally:
        controller.shutdown()


def test_stop_sends_stop_task_and_waits_for_final_status(window, ctrl, ipc):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    window.stop_button.click()
    assert ("stop_task", "T1") in ipc.calls
    assert window.status_label.text() == "Остановка…" and ctrl.timer.isActive()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
    ctrl.poll()  # после остановки Server отдаёт completed с собранным
    assert window.count_label.text() == "Объявлений: 3"


def test_stop_error_is_shown_and_polling_continues(window, ctrl, ipc, dialogs):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.stop_reply = {"type": "error", "code": "stop_failed", "message": "Task T1 not found"}
    window.stop_button.click()
    assert "Task T1 not found" in dialogs.messages[-1] and ctrl.timer.isActive()
    ipc.stop_reply = IpcConnectionError("down")
    window.stop_button.click()
    assert dialogs.messages[-1] == SERVER_DOWN_MESSAGE


def test_stop_without_task_does_nothing(ctrl, ipc):
    ctrl.stop()
    assert ipc.calls == []


def test_failed_state_shows_reasons(window, ctrl, ipc, dialogs):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "failed",
                           "errors": [{"site": "avito.ru", "kind": "captcha"},
                                      {"site": "drom.ru", "kind": "blocked"}]}]
    ctrl.poll()
    assert "avito.ru: captcha" in dialogs.messages[-1] and "drom.ru: blocked" in dialogs.messages[-1]
    assert window.status_label.text() == "Сбор завершился ошибкой"
    assert not ctrl.timer.isActive() and window.evaluate_button.isEnabled()
    assert window.count_label.text() == "Объявлений: —"


def test_cancelled_state(window, ctrl, ipc, dialogs):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "cancelled"}]
    ctrl.poll()
    assert window.status_label.text() == "Задание отменено" and dialogs.messages == []
    assert not ctrl.timer.isActive()


def test_poll_tolerates_transient_failures_then_gives_up(window, ctrl, ipc, dialogs):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.status_replies = [IpcTimeoutError("t")]
    for _ in range(MAX_POLL_FAILURES - 1):
        ctrl.poll()
    assert ctrl.timer.isActive() and dialogs.messages == []
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "running"}]
    ctrl.poll()  # успешный опрос сбрасывает счётчик
    ipc.status_replies = [IpcConnectionError("x")]
    for _ in range(MAX_POLL_FAILURES - 1):
        ctrl.poll()
    assert ctrl.timer.isActive()
    ctrl.poll()
    assert not ctrl.timer.isActive() and "Связь с сервером потеряна" in dialogs.messages[-1]
    assert window.evaluate_button.isEnabled()


def test_unknown_task_reply_stops_polling(window, ctrl, ipc, dialogs):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    ipc.status_replies = [{"type": "error", "code": "unknown_task", "message": "Task T1 not found"}]
    ctrl.poll()
    assert not ctrl.timer.isActive() and "Task T1 not found" in dialogs.messages[-1]


def test_poll_without_task_stops_timer(ctrl, ipc):
    ctrl.timer.start()
    ctrl.poll()
    assert not ctrl.timer.isActive() and ipc.calls == []


def test_shutdown_stops_timer(window, ctrl):
    pick_bmw_x5(window)
    window.evaluate_button.click()
    assert ctrl.timer.isActive()
    ctrl.shutdown()
    assert not ctrl.timer.isActive()


def test_new_submit_clears_previous_results(window, ctrl, ipc):
    window.show_results(9, {"auto.ru": 9})
    pick_bmw_x5(window)
    window.evaluate_button.click()
    assert window.count_label.text() == "Объявлений: —" and window.sites_table.rowCount() == 0


def test_default_ipc_client_is_one_shot(window, reference_db, state_db, tmp_path):
    controller = ClientController(window, Config(tmp_path / "s.json"),
                                  reference_db=reference_db, state_db=state_db)
    try:
        assert controller._ipc._one_shot is True
    finally:
        controller.shutdown()


def test_constants():
    assert controller_module.POLL_INTERVAL_MS == 2000 and MAX_POLL_FAILURES == 3


# ----------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------
def test_main_builds_window_and_controller_without_side_effects(qapp, monkeypatch, tmp_path):
    """``main()`` собирает окно и контроллер; файлы настроек/логов не трогаются."""
    from client import main as main_module

    created: dict = {}

    class FakeWindowManager:
        def __init__(self, settings_filename):
            created["settings_filename"] = settings_filename

        def apply(self, window, window_id):
            created["window_id"] = window_id

    shown: list = []
    monkeypatch.setattr(main_module, "Config", lambda: Config(tmp_path / "settings.json"))
    monkeypatch.setattr(main_module, "setup_logging", lambda **kw: created.setdefault("logging", kw))
    monkeypatch.setattr(main_module, "WindowManager", FakeWindowManager)
    monkeypatch.setattr(MainWindow, "show", lambda self: shown.append(self))
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert main_module.main(["aprice-client"]) == 0
    assert created["settings_filename"] == "aprice_client_settings.ini"
    assert created["window_id"] == "client_main"
    assert created["logging"]["component"] == "client"
    assert len(shown) == 1 and isinstance(shown[0], MainWindow)
