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

from common import constants
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


class FakeSession:
    """Подмена долгоживущего ``IpcClient`` («сеанс» Клиента, 4.3)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply: object = {"type": "active_task", "request_id": "r", "task_id": None}
        self.closed = 0
        self.close_error: Optional[Exception] = None

    def get_active_task(self, session: bool = False):
        self.calls.append(("get_active_task", session))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


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


@pytest.fixture(autouse=True)
def history_file(tmp_path, monkeypatch) -> Path:
    """История запросов — во временный файл: тесты не трогают настоящий data/."""
    path = tmp_path / "history.json"
    monkeypatch.setattr(constants, "CLIENT_HISTORY_FILE", path)
    return path


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
    store.save_task("T1", {"mode": "range"}, {}, "completed")
    store.save_ads("T1", "auto.ru", [
        {"url": "https://auto.ru/1", "price": 5_000_000, "year": 2020},
        {"url": "https://auto.ru/2", "price": 5_500_000, "year": 2020},
    ])
    store.save_ads("T1", "drom.ru", [{"url": "https://drom.ru/1.html", "price": 4_900_000}])
    store.save_task("T2", {"mode": "range"}, {}, "completed")
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
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture()
def ctrl(window, ipc, session, reference_db, state_db, tmp_path):
    controller = ClientController(
        window, Config(tmp_path / "settings.json"), ipc=ipc,
        reference_db=reference_db, state_db=state_db, session_ipc=session,
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
    """Марка BMW, модель X5; ``year`` — точный год (``год от`` = ``год до``)."""
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    if year:
        window.year_from_spin.setValue(int(year))
        window.year_to_spin.setValue(int(year))


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
def test_form_has_no_exact_mode_fields():
    """4.6: у формы нет полей режима «Точный автомобиль»."""
    names = set(FormState.__dataclass_fields__)
    assert names == {"manufacturer", "model", "year_from", "year_to", "volume_from",
                     "volume_to", "transmissions", "fuels"}
    assert not hasattr(form, "MODE_EXACT")


def test_form_exact_year_is_a_range_with_equal_bounds():
    """Точный год — это «Год от» = «Год до»; spec всегда с mode == range."""
    spec = form.build_spec(FormState(manufacturer="BMW", model="X5", year_from=2020, year_to=2020,
                                     transmissions=["АКПП"], fuels=["Бензин"]))
    assert spec == {
        "mode": "range", "manufacturer": "BMW", "model": "X5", "year_from": 2020,
        "year_to": 2020, "transmissions": ["АКПП"], "fuels": ["Бензин"],
    }
    from server import protocol
    protocol.validate_spec(spec)


def test_form_range_spec_matches_server_contract():
    spec = form.build_spec(FormState(
        manufacturer="BMW", model="X5", year_from=2018, year_to=2021,
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
    spec = form.build_spec(FormState(manufacturer="BMW", model="X5",
                                     year_from=2018, year_to=2018))
    assert "engine_volume_from" not in spec and "engine_volume_to" not in spec


@pytest.mark.parametrize("state, message", [
    (FormState(manufacturer="", model="X5", year_from=2020, year_to=2020), "марку"),
    (FormState(manufacturer="BMW", model="  ", year_from=2020, year_to=2020), "модель"),
    (FormState(manufacturer="BMW", model="X5", year_from=None, year_to=2020), "диапазон"),
    (FormState(manufacturer="BMW", model="X5"), "диапазон"),
    (FormState(manufacturer="BMW", model="X5", year_from=2021, year_to=2018), "больше"),
    (FormState(manufacturer="BMW", model="X5", year_from=2018, year_to=2021,
               volume_from=3.0, volume_to=2.0), "Объём"),
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


def test_window_has_no_mode_selection(window):
    """4.6: ни радиокнопок, ни переключения форм, ни полей точного режима."""
    for name in ("radio_exact", "radio_range", "exact_box", "generation_combo", "year_combo",
                 "transmission_combo", "fuel_combo", "set_mode", "set_years", "set_generations"):
        assert not hasattr(window, name), name
    assert window.range_box.isVisibleTo(window)  # единственная форма — всегда видна
    assert window.range_box.title() == "Параметры поиска"


def test_evaluate_enabled_with_make_and_model_and_form_has_only_ranges(window):
    """4.6: марка + модель -> «Оценить» активна; форма содержит только диапазоны."""
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
    assert not window.evaluate_button.isEnabled()
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    assert window.evaluate_button.isEnabled()  # без выбора режима/года/поколения
    assert set(vars(window.form_state())) == {
        "manufacturer", "model", "year_from", "year_to", "volume_from", "volume_to",
        "transmissions", "fuels"}
    assert set(window.transmission_checks) == set(form.TRANSMISSIONS)
    assert set(window.fuel_checks) == set(form.FUELS)
    assert form.build_spec(window.form_state())["mode"] == "range"


def test_evaluate_requires_manufacturer_and_model(window):
    window.set_manufacturers(["BMW", "Audi"])
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
    assert window.evaluate_button.isEnabled()
    window.year_from_spin.setValue(window.year_to_spin.value())
    window.year_to_spin.setValue(window.year_from_spin.value() - 5)
    assert not window.evaluate_button.isEnabled()  # «от» больше «до»


def test_form_state_reads_widgets(window):
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    window.year_from_spin.setValue(2015)
    window.year_to_spin.setValue(2019)
    window.transmission_checks["Робот"].setChecked(True)
    window.fuel_checks["Дизель"].setChecked(True)
    window.volume_from_spin.setValue(2.5)
    state = window.form_state()
    assert (state.manufacturer, state.model) == ("BMW", "X5")
    assert (state.year_from, state.year_to) == (2015, 2019)
    assert state.transmissions == ["Робот"] and state.fuels == ["Дизель"]
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


def test_buttons_follow_running_state_and_progress_is_indeterminate(window):
    window.set_manufacturers(["BMW"])
    window.set_models(["X5"])
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
    assert not window.evaluate_button.isEnabled()  # модель ещё не выбрана
    choose(window.model_combo, "X5")
    assert window.evaluate_button.isEnabled()  # марка + модель — достаточно (4.6)


def test_model_selection_does_not_touch_range_form(window, ctrl):
    """Выбор модели не меняет диапазоны и чекбоксы (поколений/годов из справочника больше нет)."""
    choose(window.manufacturer_combo, "BMW")
    window.year_from_spin.setValue(2015)
    window.year_to_spin.setValue(2018)
    window.fuel_checks["Дизель"].setChecked(True)
    choose(window.model_combo, "X5")
    state = window.form_state()
    assert (state.year_from, state.year_to, state.fuels) == (2015, 2018, ["Дизель"])


def test_changing_manufacturer_resets_model(window, ctrl):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    choose(window.manufacturer_combo, "Audi")
    assert window.form_state().model == ""
    assert items(window.model_combo) == [PLACEHOLDER, "A4", "A6"]
    assert not window.evaluate_button.isEnabled()


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
    window.transmission_checks["АКПП"].setChecked(True)
    window.evaluate_button.click()
    assert ipc.calls == [("submit_task", {
        "mode": "range", "manufacturer": "BMW", "model": "X5", "year_from": 2020,
        "year_to": 2020, "transmissions": ["АКПП"], "fuels": [],
    }, {"max_pages_per_site": 10, "max_ads_per_site": 500})]  # лимиты из настроек (4.4)
    assert ctrl.task_id == "T1"
    assert ctrl.timer.isActive() and ctrl.timer.interval() == POLL_INTERVAL_MS == 2000
    assert window.status_label.text() == "Задание принято"
    assert window.stop_button.isEnabled() and not window.evaluate_button.isEnabled()


def test_submit_range_mode_spec(window, ctrl, ipc):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
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

    monkeypatch.setattr(main_module, "ensure_database", lambda *a, **k: True)  # 5.5: сеть не нужна

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
    monkeypatch.setattr(ClientController, "start_session",
                        lambda self: created.setdefault("session_started", True))
    assert main_module.main(["aprice-client"]) == 0
    assert created["session_started"] is True
    assert shown[0].vpn_banner.isVisibleTo(shown[0])  # по умолчанию show_vpn_warning = true
    assert created["settings_filename"] == "aprice_client_settings.ini"
    assert created["window_id"] == "client_main"
    assert created["logging"]["component"] == "client"
    assert len(shown) == 1 and isinstance(shown[0], MainWindow)


# ----------------------------------------------------------------------
# 4.2: статистика в окне, история запросов
# ----------------------------------------------------------------------
from client.gui import HISTORY_PLACEHOLDER, WARNING_STYLE, format_price  # noqa: E402
from client.history import describe, load_history  # noqa: E402


def stat_cells(window) -> list[str]:
    return [window.stats_table.item(r, 1).text() for r in range(len(STAT_ROWS))]


def run_task(window, ctrl, ipc, task_id="T1") -> None:
    """Режим А: BMW X5, год 2019; сбор завершается ``completed``."""
    pick_bmw_x5(window, "2019")
    ipc.submit_reply = {"type": "task_accepted", "task_id": task_id, "queue_position": 0}
    ipc.status_replies = [{"type": "status", "task_id": task_id, "state": "completed"}]
    ctrl.submit()
    ctrl.poll()


@pytest.mark.parametrize("value,text", [
    (5_640_000, "5 640 000 ₽"), (5_640_000.4, "5 640 000 ₽"), (999, "999 ₽"),
    (1_234_567.5, "1 234 568 ₽"), (6_500_000.5, "6 500 001 ₽"), (None, NO_VALUE),
])
def test_format_price(value, text):
    assert format_price(value) == text


def test_update_stats_fills_table_and_shows_red_warning(window):
    window.update_stats({"min": 4_200_000, "max": 9_050_000, "mean": 6_500_000.5,
                         "median": 6_800_000.0, "trimmed_mean": None,
                         "warning": "Мало данных (N < 10). Оценка может быть неточной."})
    assert stat_cells(window) == ["4 200 000 ₽", "9 050 000 ₽", "6 500 001 ₽",
                                  "6 800 000 ₽", NO_VALUE]
    assert window.warning_label.text().startswith("Мало данных")
    assert window.warning_label.isVisibleTo(window)
    assert "red" in window.warning_label.styleSheet() == WARNING_STYLE


def test_no_warning_hides_label_and_clear_resets(window):
    window.update_stats({"min": 1, "warning": "x"})
    window.update_stats({"min": 1, "max": 2, "mean": 1.5, "median": 1.5,
                         "trimmed_mean": 1.5, "warning": None})
    assert window.warning_label.text() == "" and not window.warning_label.isVisibleTo(window)
    window.update_stats({"warning": "x"})
    window.clear_results()
    assert stat_cells(window) == [NO_VALUE] * 5 and window.warning_label.text() == ""


def test_completed_fills_stats_warning_and_saves_history(window, ctrl, ipc, history_file):
    run_task(window, ctrl, ipc)
    # T1: 5 000 000, 5 500 000, 4 900 000 -> N = 3 < 10
    assert stat_cells(window) == ["4 900 000 ₽", "5 500 000 ₽", "5 133 333 ₽",
                                  "5 000 000 ₽", "5 133 333 ₽"]
    assert window.warning_label.text().startswith("Мало данных (N < 10)")
    saved = load_history(history_file)
    assert len(saved) == 1
    assert saved[0]["count"] == 3 and saved[0]["min"] == 4_900_000
    assert saved[0]["spec"] == ipc.calls[0][1]  # ровно тот spec, что ушёл на Server
    assert "ads" not in saved[0] and "url" not in str(saved[0])  # объявления не хранятся
    assert items(window.history_combo) == [HISTORY_PLACEHOLDER, describe(saved[0])]


def test_history_combo_disabled_while_running_and_enabled_after(window, ctrl, ipc):
    assert not window.history_combo.isEnabled()  # истории нет
    run_task(window, ctrl, ipc)
    assert window.history_combo.isEnabled()
    pick_bmw_x5(window, "2019")
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T1", "queue_position": 0}
    ctrl.submit()
    assert not window.history_combo.isEnabled()


def test_empty_result_is_not_saved(window, ctrl, ipc, history_file):
    pick_bmw_x5(window, "2019")
    ipc.submit_reply = {"type": "task_accepted", "task_id": "NOPE", "queue_position": 0}
    ipc.status_replies = [{"type": "status", "task_id": "NOPE", "state": "completed"}]
    ctrl.submit()
    ctrl.poll()
    assert window.warning_label.text() == "Нет данных для оценки."
    assert load_history(history_file) == []
    assert items(window.history_combo) == [HISTORY_PLACEHOLDER]


def test_history_write_error_does_not_break_results(window, ctrl, ipc, dialogs, monkeypatch):
    monkeypatch.setattr(controller_module, "save_to_history",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("диск полон")))
    run_task(window, ctrl, ipc)
    assert window.count_label.text() == "Объявлений: 3" and stat_cells(window)[0] == "4 900 000 ₽"
    assert dialogs.messages == [] and window.status_label.text().startswith("Готово")


def test_history_selection_restores_form_without_new_collection(window, ctrl, ipc):
    pick_bmw_x5(window, "2019")
    window.transmission_checks["АКПП"].setChecked(True)
    window.fuel_checks["Бензин"].setChecked(True)
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T1", "queue_position": 0}
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
    ctrl.submit()
    ctrl.poll()
    calls_before = list(ipc.calls)

    choose(window.manufacturer_combo, "Audi")  # пользователь ушёл в другую форму
    choose(window.model_combo, "A4")
    window.clear_results()
    choose(window.history_combo, items(window.history_combo)[1])  # выбор записи

    state = window.form_state()
    assert (state.manufacturer, state.model) == ("BMW", "X5")
    assert (state.year_from, state.year_to) == (2019, 2019)
    assert (state.transmissions, state.fuels) == (["АКПП"], ["Бензин"])
    assert stat_cells(window)[0] == "4 900 000 ₽"
    assert window.count_label.text() == "Объявлений: 3"
    assert window.sites_table.rowCount() == 0
    assert ipc.calls == calls_before  # новый сбор НЕ запускался
    assert ctrl.task_id is None and not ctrl.timer.isActive()
    assert window.status_label.text().startswith("Сохранённый результат")


@pytest.mark.parametrize("legacy_spec", [
    {"mode": "exact", "manufacturer": "BMW", "model": "X5", "generation": "IV (G05)",
     "year_from": 2019, "year_to": 2019, "transmissions": ["АКПП"], "fuels": ["Бензин"]},
    {"mode": "exact", "manufacturer": "BMW", "model": "X5", "year_from": 2019,  # без year_to
     "transmissions": ["АКПП"], "fuels": ["Бензин"]},
    {"mode": "exact", "manufacturer": "BMW", "model": "X5", "year_to": 2019,  # без year_from
     "transmissions": ["АКПП"], "fuels": ["Бензин"]},
])
def test_legacy_exact_history_entry_is_shown_as_equal_year_range(window, ctrl, history_file, legacy_spec):
    """Старая запись истории с mode == exact не роняет Client: год от = год до."""
    from client.history import save_to_history
    from client.stats import calculate_stats
    save_to_history(calculate_stats([1_000_000 + i for i in range(25)]), legacy_spec)
    ctrl.refresh_history()
    choose(window.history_combo, items(window.history_combo)[1])
    state = window.form_state()
    assert (state.manufacturer, state.model) == ("BMW", "X5")
    assert (state.year_from, state.year_to) == (2019, 2019)
    assert (state.transmissions, state.fuels) == (["АКПП"], ["Бензин"])
    assert window.count_label.text() == "Объявлений: 25"


def test_history_selection_restores_range_form(window, ctrl, ipc, history_file):
    from client.history import save_to_history
    from client.stats import calculate_stats
    spec = {"mode": "range", "manufacturer": "BMW", "model": "X5", "year_from": 2015,
            "year_to": 2019, "transmissions": ["АКПП", "Робот"], "fuels": ["Дизель"],
            "engine_volume_from": 2.0, "engine_volume_to": 3.5}
    save_to_history(calculate_stats([1_000_000 + i for i in range(25)]), spec)
    ctrl.refresh_history()
    choose(window.history_combo, items(window.history_combo)[1])
    state = window.form_state()
    assert (state.manufacturer, state.model) == ("BMW", "X5")
    assert (state.year_from, state.year_to) == (2015, 2019)
    assert (state.volume_from, state.volume_to) == (2.0, 3.5)
    assert sorted(state.transmissions) == ["АКПП", "Робот"] and state.fuels == ["Дизель"]
    assert window.warning_label.text() == ""  # N = 25 — без предупреждения
    assert window.count_label.text() == "Объявлений: 25"


def test_history_selection_ignored_while_collecting(window, ctrl, ipc):
    run_task(window, ctrl, ipc)
    pick_bmw_x5(window, "2019")
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T1", "queue_position": 0}
    ctrl.submit()
    window.clear_results()
    ctrl.on_history_selected(0)  # даже если сигнал дошёл
    assert stat_cells(window) == [NO_VALUE] * 5
    ctrl.on_history_selected(7)  # и индекс вне диапазона безопасен
    ctrl.on_history_selected(-1)


def test_placeholder_selection_does_nothing(window, ctrl, ipc):
    run_task(window, ctrl, ipc)
    window.clear_results()
    choose(window.history_combo, HISTORY_PLACEHOLDER)
    assert stat_cells(window) == [NO_VALUE] * 5


def test_history_survives_restart_and_is_limited_to_ten(window, ctrl, ipc, qapp, reference_db,
                                                         state_db, tmp_path):
    for _ in range(12):
        run_task(window, ctrl, ipc)
    assert len(items(window.history_combo)) == 1 + 10
    other = MainWindow()
    ClientController(other, Config(tmp_path / "s.json"), ipc=FakeIpc(),
                     reference_db=reference_db, state_db=state_db)
    assert len(items(other.history_combo)) == 11  # история прочитана при старте


def test_priced_count_shown_when_some_ads_have_no_price(window):
    window.show_results(5, {"auto.ru": 5}, 3)
    assert window.count_label.text() == "Объявлений: 5 (с ценой: 3)"
    window.show_results(5, {"auto.ru": 5}, 5)
    assert window.count_label.text() == "Объявлений: 5"


# ----------------------------------------------------------------------
# 4.3: графики, VPN-плашка, жизненный цикл
# ----------------------------------------------------------------------
from client.gui import TAB_CHARTS, TAB_RESULTS, VPN_TEXT  # noqa: E402


def test_vpn_banner_visible_by_default_with_text_and_style(qapp):
    win = MainWindow()
    assert win.vpn_banner.isVisibleTo(win)
    assert win.vpn_label.text() == VPN_TEXT
    assert "#fff3cd" in win.vpn_label.styleSheet()
    assert win.vpn_timer.isActive() and win.vpn_timer.interval() == 10_000


def test_vpn_banner_hidden_when_disabled(qapp):
    win = MainWindow(show_vpn_warning=False)
    assert not win.vpn_banner.isVisibleTo(win) and not win.vpn_timer.isActive()


def test_vpn_banner_close_button_hides_it(qapp):
    win = MainWindow()
    win.vpn_close_button.click()
    assert not win.vpn_banner.isVisibleTo(win) and not win.vpn_timer.isActive()


def test_vpn_banner_hides_itself_by_timer(qapp):
    win = MainWindow(vpn_banner_ms=30)
    assert win.vpn_banner.isVisibleTo(win)
    pump_until(lambda: not win.vpn_banner.isVisibleTo(win))


def test_main_respects_show_vpn_warning_setting(monkeypatch, tmp_path):
    from client import main as main_module

    monkeypatch.setattr(main_module, "ensure_database", lambda *a, **k: True)  # 5.5: сеть не нужна

    config = Config(tmp_path / "settings.json")
    config.set("client", "show_vpn_warning", False)
    monkeypatch.setattr(main_module, "Config", lambda: type("C", (), {"load": lambda self: config})())
    monkeypatch.setattr(main_module, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(main_module, "WindowManager", lambda **kw: type(
        "W", (), {"apply": lambda self, *a: None})())
    monkeypatch.setattr(ClientController, "start_session", lambda self: False)
    shown: list = []
    monkeypatch.setattr(MainWindow, "show", lambda self: shown.append(self))
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert main_module.main(["aprice-client"]) == 0
    assert not shown[0].vpn_banner.isVisibleTo(shown[0])


def test_default_settings_enable_vpn_warning():
    assert constants.DEFAULT_SETTINGS["client"]["show_vpn_warning"] is True


def test_charts_tab_present(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [TAB_RESULTS, TAB_CHARTS]
    assert window.tabs.indexOf(window.chart_widget) == 1


def test_completed_draws_charts_and_clear_resets_them(window, ctrl, ipc):
    run_task(window, ctrl, ipc)  # T1: три цены
    figure = window.chart_widget.figure
    assert len(figure.axes) == 2
    hist = figure.axes[0]
    assert sum(p.get_height() for p in hist.patches) == 3
    window.clear_results()
    assert len(figure.axes) == 1


def test_history_view_clears_charts(window, ctrl, ipc):
    run_task(window, ctrl, ipc)
    choose(window.history_combo, items(window.history_combo)[1])
    assert len(window.chart_widget.figure.axes) == 1  # цены в истории не хранятся


SPEC_ACTIVE = {"mode": "range", "manufacturer": "BMW", "model": "X5",
               "year_from": 2019, "year_to": 2019, "transmissions": ["АКПП"], "fuels": ["Бензин"]}


def test_start_session_opens_session_and_without_task_does_nothing(window, ctrl, session):
    assert ctrl.start_session() is False
    assert session.calls == [("get_active_task", True)]
    assert ctrl.task_id is None and not ctrl.timer.isActive()
    assert window.evaluate_button.isEnabled() is False  # форма пуста, но не «занято»
    assert not window.stop_button.isEnabled()


def test_start_session_adopts_active_task(window, ctrl, ipc, session):
    session.reply = {"type": "active_task", "task_id": "T1", "state": "running", "spec": SPEC_ACTIVE}
    assert ctrl.start_session() is True
    assert ctrl.task_id == "T1" and ctrl.timer.isActive()
    assert window.stop_button.isEnabled() and not window.evaluate_button.isEnabled()
    state = window.form_state()
    assert (state.manufacturer, state.model) == ("BMW", "X5")
    assert (state.year_from, state.year_to) == (2019, 2019)
    assert (state.transmissions, state.fuels) == (["АКПП"], ["Бензин"])
    assert "Подхвачено" in window.status_label.text()
    assert "submit_task" not in ipc.names()  # новое задание не отправляется


def test_adopted_task_completes_into_stats_and_history(window, ctrl, ipc, session, history_file):
    session.reply = {"type": "active_task", "task_id": "T1", "state": "running", "spec": SPEC_ACTIVE}
    ctrl.start_session()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
    ctrl.poll()
    assert ipc.calls[-1] == ("get_status", "T1")
    assert window.count_label.text() == "Объявлений: 3" and stat_cells(window)[0] == "4 900 000 ₽"
    assert load_history(history_file)[0]["spec"] == SPEC_ACTIVE
    assert ctrl.task_id is None and not ctrl.timer.isActive()


def test_adopt_with_missing_spec_still_polls(window, ctrl, ipc, session):
    session.reply = {"type": "active_task", "task_id": "T1", "state": "queued"}
    assert ctrl.start_session() is True and ctrl.timer.isActive()


def test_start_session_server_down_is_silent(window, ctrl, session, dialogs):
    session.reply = IpcConnectionError("нет связи")
    assert ctrl.start_session() is False
    assert dialogs.messages == [] and ctrl.task_id is None


def test_start_session_error_reply_is_ignored(window, ctrl, session, dialogs):
    session.reply = {"type": "error", "code": "unknown_type", "message": "старый Server"}
    assert ctrl.start_session() is False and dialogs.messages == []


def test_submit_opens_session_once_when_server_was_down_at_start(window, ctrl, ipc, session):
    session.reply = IpcConnectionError("нет связи")
    ctrl.start_session()
    session.reply = {"type": "active_task", "task_id": "T1", "state": "queued", "spec": {}}
    pick_bmw_x5(window, "2019")
    ctrl.submit()
    assert session.calls == [("get_active_task", True)] * 2  # старт (неудача) + submit
    assert ctrl.task_id == "T1" and window.stop_button.isEnabled()  # ответ сеанса не «подхватывается»
    ctrl.stop()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
    ctrl.poll()
    pick_bmw_x5(window, "2019")
    ctrl.submit()
    assert len(session.calls) == 2  # сеанс уже открыт — повторно не открывается


def test_shutdown_closes_session(window, ctrl, session):
    ctrl.start_session()
    ctrl.shutdown()
    assert session.closed == 1 and not ctrl.timer.isActive()


def test_shutdown_survives_close_error(window, ctrl, session):
    ctrl.start_session()
    session.close_error = OSError("broken pipe")
    ctrl.shutdown()  # не падает
    assert session.closed == 1


def test_active_task_not_adopted_over_own_task(window, ctrl, ipc, session):
    pick_bmw_x5(window, "2019")
    ctrl.submit()
    assert ctrl.task_id == "T1"
    session.reply = {"type": "active_task", "task_id": "OTHER", "state": "running", "spec": {}}
    ctrl._session_open = False  # как будто сеанс ещё не открывался
    assert ctrl.start_session() is False and ctrl.task_id == "T1"


# ----------------------------------------------------------------------
# 4.4: экспорт, настройки, обновление справочника
# ----------------------------------------------------------------------
import csv  # noqa: E402

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402

from client.export import HEADERS, ExportError  # noqa: E402
from client.gui import with_export_suffix  # noqa: E402


def read_csv(path) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh, delimiter=";"))


def test_export_and_settings_buttons_exist(window):
    assert window.export_button.text() == "Экспорт" and window.settings_button.text() == "Настройки"
    assert not window.export_button.isEnabled()  # выгружать пока нечего
    assert window.settings_button.isEnabled()


def test_export_button_enabled_after_results_and_reset_by_new_task_and_history(window, ctrl, ipc):
    run_task(window, ctrl, ipc)
    assert window.export_button.isEnabled() and len(ctrl._last_ads) == 3
    choose(window.history_combo, items(window.history_combo)[1])
    assert not window.export_button.isEnabled() and ctrl._last_ads == []
    run_task(window, ctrl, ipc)
    assert window.export_button.isEnabled()
    pick_bmw_x5(window, "2019")
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T1", "queue_position": 0}
    ctrl.submit()
    assert not window.export_button.isEnabled() and ctrl._last_ads == []


def test_export_button_click_writes_raw_ads(window, ctrl, ipc, tmp_path, monkeypatch):
    run_task(window, ctrl, ipc)
    target = tmp_path / "out" / "result.csv"
    monkeypatch.setattr(window, "choose_export_path", lambda: str(target))
    window.export_button.click()
    rows = read_csv(target)
    assert rows[0] == list(HEADERS) and len(rows) == 4  # заголовок + 3 объявления
    assert {r[0] for r in rows[1:]} == {"auto.ru", "drom.ru"}
    assert sorted(int(r[4]) for r in rows[1:]) == [4_900_000, 5_000_000, 5_500_000]
    assert "https://auto.ru/1" in {r[6] for r in rows[1:]}
    assert window.status_label.text() == f"Экспортировано объявлений: 3 → {target}"


def test_export_xlsx_via_controller(window, ctrl, ipc, tmp_path):
    import pandas as pd
    run_task(window, ctrl, ipc)
    assert ctrl.export_data(tmp_path / "r.xlsx") == 3
    frame = pd.read_excel(tmp_path / "r.xlsx", engine="openpyxl")
    assert list(frame.columns) == list(HEADERS) and len(frame) == 3


def test_export_data_accepts_explicit_ads(ctrl, tmp_path):
    assert ctrl.export_data(tmp_path / "e.csv", [{"site": "s", "price": 1}]) == 1
    assert read_csv(tmp_path / "e.csv")[1][0] == "s"


def test_export_without_data_shows_error(window, ctrl, dialogs, monkeypatch):
    monkeypatch.setattr(window, "choose_export_path", lambda: pytest.fail("диалог не должен открываться"))
    ctrl.on_export_requested()
    assert dialogs.messages == ["Нет данных для экспорта: сначала выполните оценку."]


def test_export_cancelled_in_file_dialog_does_nothing(window, ctrl, ipc, tmp_path, monkeypatch):
    run_task(window, ctrl, ipc)
    before = window.status_label.text()
    monkeypatch.setattr(window, "choose_export_path", lambda: None)
    ctrl.on_export_requested()
    assert window.status_label.text() == before and list(tmp_path.glob("*.csv")) == []


def test_export_error_is_shown_and_status_unchanged(window, ctrl, ipc, dialogs, tmp_path, monkeypatch):
    run_task(window, ctrl, ipc)
    before = window.status_label.text()
    monkeypatch.setattr(window, "choose_export_path", lambda: str(tmp_path / "bad.txt"))
    ctrl.on_export_requested()
    assert "формат" in dialogs.messages[-1] and window.status_label.text() == before
    (tmp_path / "busy.csv").mkdir()
    monkeypatch.setattr(window, "choose_export_path", lambda: str(tmp_path / "busy.csv"))
    ctrl.on_export_requested()
    assert "Не удалось записать" in dialogs.messages[-1]


def test_export_data_raises_export_error(ctrl, tmp_path):
    with pytest.raises(ExportError):
        ctrl.export_data(tmp_path / "x.doc", [])


@pytest.mark.parametrize("path,flt,expected", [
    ("/a/b/report", "CSV (*.csv)", "/a/b/report.csv"),
    ("/a/b/report", "Excel (*.xlsx)", "/a/b/report.xlsx"),
    ("/a/b/report", "", "/a/b/report.csv"),
    ("/a/b/report.xlsx", "CSV (*.csv)", "/a/b/report.xlsx"),  # явное расширение главнее фильтра
    ("/a/b/report.CSV", "Excel (*.xlsx)", "/a/b/report.CSV"),
])
def test_with_export_suffix(path, flt, expected):
    assert with_export_suffix(path, flt) == expected


def test_choose_export_path_uses_dialog(window, monkeypatch):
    results = iter([("/tmp/out", "Excel (*.xlsx)"), ("", "")])
    seen = []

    class FakeFileDialog:
        @staticmethod
        def getSaveFileName(parent, caption, directory, flt):
            seen.append((directory, flt))
            return next(results)

    monkeypatch.setattr(gui_module, "QFileDialog", FakeFileDialog)
    assert window.choose_export_path() == "/tmp/out.xlsx"
    assert window.choose_export_path() is None  # «Отмена»
    assert seen[0] == ("aprice_export.csv", "CSV (*.csv);;Excel (*.xlsx)")


# ---- настройки ----
class FakeDialog(QObject):
    database_updated = pyqtSignal(object)
    instances: list = []

    def __init__(self, config, parent, db_path=None):
        super().__init__()
        self.args = (config, parent, db_path)
        self.executed = 0
        FakeDialog.instances.append(self)

    def exec(self):
        self.executed += 1
        return 1


class FakeWindowManager:
    def __init__(self):
        self.applied: list = []

    def apply(self, widget, window_id):
        self.applied.append((widget, window_id))


@pytest.fixture()
def settings_ctrl(window, ipc, session, reference_db, state_db, tmp_path):
    FakeDialog.instances = []
    wm = FakeWindowManager()
    controller = ClientController(
        window, Config(tmp_path / "settings.json"), ipc=ipc, reference_db=reference_db,
        state_db=state_db, session_ipc=session, window_manager=wm,
        settings_dialog_factory=FakeDialog,
    )
    controller.wm = wm
    yield controller
    controller.shutdown()


def test_settings_button_opens_modal_dialog_positioned_by_window_manager(window, settings_ctrl, reference_db):
    window.settings_button.click()
    dialog = FakeDialog.instances[0]
    assert dialog.executed == 1
    assert dialog.args == (settings_ctrl._config, window, reference_db)
    assert settings_ctrl.wm.applied == [(dialog, "settings_dialog")]


def test_settings_without_window_manager(window, ipc, session, reference_db, state_db, tmp_path):
    FakeDialog.instances = []
    controller = ClientController(window, Config(tmp_path / "s.json"), ipc=ipc,
                                  reference_db=reference_db, state_db=state_db, session_ipc=session,
                                  settings_dialog_factory=FakeDialog)
    controller.open_settings()
    assert FakeDialog.instances[0].executed == 1
    controller.shutdown()


def test_default_dialog_is_settings_dialog(window, ctrl, monkeypatch):
    import client.settings_dialog as sd
    created = []

    class Stub(FakeDialog):
        def __init__(self, config, parent, db_path=None):
            super().__init__(config, parent, db_path)
            created.append(self)

    monkeypatch.setattr(sd, "SettingsDialog", Stub)
    ctrl.open_settings()
    assert created and created[0].executed == 1


def test_limits_from_settings_go_to_server(window, ctrl, ipc):
    ctrl._config.set("client", "max_pages_per_site", 3)
    ctrl._config.set("client", "max_ads_per_site", 120)
    pick_bmw_x5(window, "2019")
    ctrl.submit()
    assert ipc.calls[0][2] == {"max_pages_per_site": 3, "max_ads_per_site": 120}


@pytest.mark.parametrize("bad", [0, -5, "10", None, True, 1.5])
def test_invalid_limits_are_not_sent(ctrl, bad):
    ctrl._config.set("client", "max_pages_per_site", bad)
    ctrl._config.set("client", "max_ads_per_site", bad)
    assert ctrl._limit_options() == {}


def add_manufacturer(path: Path, name: str) -> None:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO manufacturers (name, updated_at) VALUES (?, 't')", (name,))
    conn.execute("INSERT INTO models (manufacturer_id, name, updated_at) VALUES (?, '001', 't')",
                 (cur.lastrowid,))  # марка без моделей в списке не появляется
    conn.commit()
    conn.close()


def test_database_update_reloads_reference_and_keeps_form(window, settings_ctrl, reference_db):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    window.year_from_spin.setValue(2019)
    window.year_to_spin.setValue(2019)
    add_manufacturer(reference_db, "Zeekr")
    settings_ctrl._on_database_updated(object())
    assert "Zeekr" in items(window.manufacturer_combo)
    state = window.form_state()
    assert (state.manufacturer, state.model, state.year_from, state.year_to) == (
        "BMW", "X5", 2019, 2019)
    assert window.status_label.text() == "Справочник обновлён"


def test_database_update_keeps_range_form(window, settings_ctrl, reference_db):
    choose(window.manufacturer_combo, "BMW")
    choose(window.model_combo, "X5")
    window.year_from_spin.setValue(2015)
    window.year_to_spin.setValue(2019)
    window.volume_from_spin.setValue(2.0)
    add_manufacturer(reference_db, "Zeekr")
    settings_ctrl._on_database_updated(object())
    state = window.form_state()
    assert (state.manufacturer, state.model) == ("BMW", "X5")
    assert (state.year_from, state.year_to, state.volume_from) == (2015, 2019, 2.0)


def test_database_update_during_collection_is_deferred(window, settings_ctrl, ipc, reference_db):
    pick_bmw_x5(window, "2019")
    ipc.submit_reply = {"type": "task_accepted", "task_id": "T1", "queue_position": 0}
    settings_ctrl.submit()
    add_manufacturer(reference_db, "Zeekr")
    settings_ctrl._on_database_updated(object())
    assert "Zeekr" not in items(window.manufacturer_combo) and settings_ctrl._reload_pending
    assert "после сбора" in window.status_label.text()
    ipc.status_replies = [{"type": "status", "task_id": "T1", "state": "completed"}]
    settings_ctrl.poll()
    assert "Zeekr" in items(window.manufacturer_combo) and not settings_ctrl._reload_pending
    assert window.form_state().model == "X5"  # форма не сброшена
    assert stat_cells(window)[0] == "4 900 000 ₽"  # результаты на месте


def test_dialog_signal_triggers_reload(window, settings_ctrl, reference_db):
    window.settings_button.click()
    add_manufacturer(reference_db, "Zeekr")
    FakeDialog.instances[0].database_updated.emit(object())
    assert "Zeekr" in items(window.manufacturer_combo)


def test_main_passes_window_manager_to_controller(monkeypatch, tmp_path):
    from client import main as main_module

    monkeypatch.setattr(main_module, "ensure_database", lambda *a, **k: True)  # 5.5: сеть не нужна

    seen: dict = {}

    class SpyController(ClientController):
        def __init__(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            super().__init__(*args, **kwargs)

    class FakeManager:
        def __init__(self, settings_filename):
            seen["manager"] = self

        def apply(self, window, window_id):
            pass

    monkeypatch.setattr(main_module, "Config", lambda: Config(tmp_path / "settings.json"))
    monkeypatch.setattr(main_module, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(main_module, "WindowManager", FakeManager)
    monkeypatch.setattr(main_module, "ClientController", SpyController)
    monkeypatch.setattr(ClientController, "start_session", lambda self: False)
    monkeypatch.setattr(MainWindow, "show", lambda self: None)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert main_module.main(["aprice-client"]) == 0
    assert seen["kwargs"]["window_manager"] is seen["manager"]  # окно настроек центруется (W-01)
