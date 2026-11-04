"""Юнит-тесты контракта IPC (server/protocol.py) — без сети.

Соответствие требованиям PR-01..PR-08 и списку проверок раздела 5.1
промпта подзадачи 3.1. Запуск: ``pytest tests/test_server_protocol.py -q``.
"""

from __future__ import annotations

import json

import pytest

from server.protocol import (
    CancelTaskMsg,
    CaptchaRequiredMsg,
    ErrorMsg,
    GetStatusMsg,
    InvalidMessageError,
    StatusMsg,
    StopTaskMsg,
    SubmitTaskMsg,
    TaskAcceptedMsg,
    TaskCompletedMsg,
    TaskFailedMsg,
    TaskStoppedMsg,
    deserialize,
    serialize,
    validate_message,
    validate_options,
    validate_spec,
)

# ---------------------------------------------------------------------------
# Фабрики корректных сообщений
# ---------------------------------------------------------------------------
VALID_SPEC_EXACT = {
    "mode": "exact",
    "manufacturer": "BMW",
    "model": "X5",
    "generation": "IV (2018–2021)",
    "year_from": 2018,
    "year_to": 2018,
    "transmissions": ["АКПП"],
    "fuels": ["Бензин"],
}

VALID_SPEC_RANGE = {
    "mode": "range",
    "manufacturer": "Toyota",
    "model": "Camry",
    "year_from": 2018,
    "year_to": 2021,
    "transmissions": [],
    "fuels": [],
}

VALID_OPTIONS = {
    "sites": ["auto.ru", "drom.ru"],
    "max_ads_per_site": 500,
    "max_pages_per_site": 10,
}


def all_sample_messages() -> list:
    """По одному корректному экземпляру каждого из 11 типов сообщений."""
    return [
        SubmitTaskMsg(request_id="r1", spec=dict(VALID_SPEC_EXACT),
                      options=dict(VALID_OPTIONS)),
        GetStatusMsg(request_id="r2", task_id="t1"),
        StopTaskMsg(request_id="r3", task_id="t1"),
        CancelTaskMsg(request_id="r4", task_id="t1"),
        TaskAcceptedMsg(request_id="r1", task_id="t1", estimated_seconds=3600,
                        queue_position=0),
        StatusMsg(task_id="t1", state="running", current_site="avito.ru",
                  progress={"page": 3, "total_pages": 10, "ads_collected": 45},
                  eta_seconds=1200,
                  errors=[{"site": "auto.ru", "kind": "captcha", "ts": "2026-11-04"}]),
        StatusMsg(task_id="t1", state="queued"),  # опциональные поля = None (PR-05)
        TaskCompletedMsg(task_id="t1", total_ads=234),
        TaskFailedMsg(task_id="t1", reason="все площадки недоступны"),
        TaskStoppedMsg(task_id="t1", total_ads=123),
        CaptchaRequiredMsg(task_id="t1", site="drom.ru",
                           url="https://drom.ru/showcaptcha?t=1"),
        ErrorMsg(request_id="r9", code="invalid_spec", message="bad mode"),
    ]


# ---------------------------------------------------------------------------
# 1. Round-trip для всех типов (PR-07)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg", all_sample_messages(),
                         ids=lambda m: f"{m.type}:{type(m).__name__}")
def test_roundtrip_all_types(msg) -> None:
    """serialize -> deserialize даёт семантически эквивалентный JSON."""
    line = serialize(msg)
    assert "\n" not in line  # завершающий перевод строки добавляет транспорт
    restored = deserialize(line)
    assert type(restored) is type(msg)
    assert json.loads(serialize(restored)) == json.loads(line)
    validate_message(restored)  # восстановленное сообщение корректно


# ---------------------------------------------------------------------------
# 2. Валидные spec: exact / range проходят
# ---------------------------------------------------------------------------
def test_spec_exact_and_range_valid() -> None:
    validate_spec(dict(VALID_SPEC_EXACT))
    validate_spec(dict(VALID_SPEC_RANGE))
    deserialize(serialize(SubmitTaskMsg(request_id="r", spec=dict(VALID_SPEC_EXACT),
                                        options={})))
    deserialize(serialize(SubmitTaskMsg(request_id="r", spec=dict(VALID_SPEC_RANGE),
                                        options=dict(VALID_OPTIONS))))


# ---------------------------------------------------------------------------
# 3. Неверный mode
# ---------------------------------------------------------------------------
def test_invalid_mode() -> None:
    spec = dict(VALID_SPEC_RANGE, mode="fuzzy")
    with pytest.raises(InvalidMessageError, match="invalid_mode"):
        validate_spec(spec)
    with pytest.raises(InvalidMessageError, match="invalid_mode"):
        deserialize(json.dumps({"type": "submit_task", "request_id": "r",
                                "spec": spec, "options": {}}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 4. exact с year_from != year_to
# ---------------------------------------------------------------------------
def test_exact_year_mismatch() -> None:
    spec = dict(VALID_SPEC_RANGE, mode="exact", year_from=2018, year_to=2021)
    with pytest.raises(InvalidMessageError, match="invalid_year_range"):
        validate_spec(spec)


# ---------------------------------------------------------------------------
# 5. range с year_from > year_to
# ---------------------------------------------------------------------------
def test_range_year_inverted() -> None:
    spec = dict(VALID_SPEC_RANGE, year_from=2022, year_to=2021)
    with pytest.raises(InvalidMessageError, match="invalid_year_range"):
        validate_spec(spec)


# ---------------------------------------------------------------------------
# 6. Не-JSON
# ---------------------------------------------------------------------------
def test_deserialize_not_json() -> None:
    with pytest.raises(InvalidMessageError, match="invalid_json"):
        deserialize("not json")
    with pytest.raises(InvalidMessageError, match="invalid_json"):
        deserialize("[1, 2, 3]")  # валидный JSON, но не объект


# ---------------------------------------------------------------------------
# 7. Неизвестный type (PR-02)
# ---------------------------------------------------------------------------
def test_unknown_type() -> None:
    with pytest.raises(InvalidMessageError, match="unknown_type"):
        deserialize('{"type": "unknown"}')
    with pytest.raises(InvalidMessageError, match="unknown_type"):
        deserialize('{"request_id": "r"}')  # type отсутствует


# ---------------------------------------------------------------------------
# 8. Русские строки сохраняются (PR-08)
# ---------------------------------------------------------------------------
def test_cyrillic_preserved() -> None:
    spec = dict(VALID_SPEC_RANGE, manufacturer="Лада", model="Веста",
                transmissions=["Механика"])
    line = serialize(SubmitTaskMsg(request_id="r", spec=spec, options={}))
    assert "Лада" in line          # ensure_ascii=False — без \uXXXX
    assert "\\u041b" not in line
    restored = deserialize(line)
    assert restored.spec["manufacturer"] == "Лада"
    assert restored.spec["transmissions"] == ["Механика"]


# ---------------------------------------------------------------------------
# 9. Пропущенное обязательное поле (PR-03)
# ---------------------------------------------------------------------------
def test_missing_required_field() -> None:
    with pytest.raises(InvalidMessageError, match="missing_field"):
        deserialize('{"type": "get_status", "request_id": "r"}')
    with pytest.raises(InvalidMessageError, match="missing_field"):
        deserialize('{"type": "task_completed", "task_id": "t"}')


# ---------------------------------------------------------------------------
# 10. Неверный тип поля (PR-04)
# ---------------------------------------------------------------------------
def test_invalid_field_type() -> None:
    spec = dict(VALID_SPEC_RANGE, year_from="abc")
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        validate_spec(spec)
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        deserialize(json.dumps({"type": "get_status", "request_id": "r",
                                "task_id": 123}))  # строковое поле числом
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        deserialize(json.dumps({"type": "task_completed", "task_id": "t",
                                "total_ads": "many"}))


# ---------------------------------------------------------------------------
# Дополнительные проверки контракта
# ---------------------------------------------------------------------------
def test_serialize_rejects_non_contract_objects() -> None:
    """PR-06: serialize вне контракта / без type -> ValueError."""

    class Fake:
        type = "nope"

    with pytest.raises(ValueError):
        serialize(Fake())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        serialize({"type": "status"})  # dict, а не dataclass

    msg = StatusMsg(task_id="t", state="running")
    broken = json.loads(serialize(msg))
    broken.pop("type")
    with pytest.raises(InvalidMessageError, match="unknown_type"):
        deserialize(json.dumps(broken))


def test_options_defaults_and_sites_validation() -> None:
    """options: дефолты, пустой sites = любые, sberauto по умолчанию выключен."""
    normalized = validate_options({})
    assert normalized["max_ads_per_site"] == 500
    assert normalized["max_pages_per_site"] == 10
    assert "sberauto.com" not in normalized["sites"]  # S-15
    assert validate_options({"sites": []})["sites"] == []  # пустой = любые
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        validate_options({"sites": ["ya.ru"]})
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        validate_options({"max_pages_per_site": 0})
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        validate_options({"max_ads_per_site": "много"})


def test_state_enum_enforced() -> None:
    """StatusMsg.state ограничен FSM (раздел 2.2 промпта задачи №3)."""
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        deserialize('{"type": "status", "task_id": "t", "state": "paused"}')
    for state in ("queued", "running", "stopping", "completed", "failed", "cancelled"):
        assert deserialize(f'{{"type": "status", "task_id": "t", "state": "{state}"}}').state == state


def test_validate_message_on_built_instance() -> None:
    """validate_message ловит пустые обязательные поля у готового объекта."""
    with pytest.raises(InvalidMessageError, match="missing_field"):
        validate_message(GetStatusMsg(request_id="", task_id="t"))
    with pytest.raises(InvalidMessageError, match="invalid_field"):
        validate_message(StatusMsg(task_id="t", state="weird"))
    validate_message(TaskAcceptedMsg(request_id="r", task_id="t",
                                     estimated_seconds=1800, queue_position=1))
