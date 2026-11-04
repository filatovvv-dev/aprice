"""Контракт IPC-протокола «АвтоПрайс» (Server <-> Client).

Транспорт: TCP на ``127.0.0.1:8765``, обмен — JSON-строками, разделёнными
``\\n`` (по одному сообщению на строку). Модуль определяет dataclass'ы для
всех типов сообщений (раздел 2 промпта задачи №3), функции (де)сериализации
и валидации. Контракт «заморожен» после приёмки подзадачи 3.1: любое
изменение состава полей требует согласования с ТЗ-разработчиком.

Безопасность (PR-01): только ``json.loads`` + явный разбор полей;
никакого ``eval`` / ``exec`` / ``pickle``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# Константы контракта
# ---------------------------------------------------------------------------

#: Допустимые значения ``spec.mode``.
VALID_MODES: tuple[str, ...] = ("exact", "range")

#: Площадки, допустимые в ``options.sites`` (константы — из common.constants.SITES,
#: здесь продублированы как часть контракта без зависимости от путей проекта).
ALLOWED_SITES: frozenset[str] = frozenset(
    {"auto.ru", "drom.ru", "avito.ru", "sberauto.com", "bibika.ru"}
)

#: Площадки по умолчанию (sberauto.com выключен по умолчанию — S-15).
DEFAULT_SITES: tuple[str, ...] = ("auto.ru", "drom.ru", "avito.ru", "bibika.ru")

#: Значения ``StatusMsg.state`` (FSM задания — TaskManager, подзадача 3.4).
TASK_STATES: frozenset[str] = frozenset(
    {"queued", "running", "stopping", "completed", "failed", "cancelled"}
)

DEFAULT_MAX_ADS_PER_SITE: int = 500
DEFAULT_MAX_PAGES_PER_SITE: int = 10


# ---------------------------------------------------------------------------
# Исключение
# ---------------------------------------------------------------------------
class InvalidMessageError(ValueError):
    """Некорректное сообщение протокола.

    Текст всегда начинается с машинного кода, чтобы принимающая сторона
    могла вернуть его в ``ErrorMsg.code``:

    * ``unknown_type`` — неизвестный/отсутствующий ``type`` (PR-02);
    * ``missing_field`` — отсутствует обязательное поле (PR-03);
    * ``invalid_field`` — неверный тип или значение поля (PR-04);
    * конкретные коды валидации spec/options, напр. ``invalid_year_range``.
    """


# ---------------------------------------------------------------------------
# Dataclass'ы сообщений (раздел 2.1 промпта задачи №3)
# ---------------------------------------------------------------------------
@dataclass
class SubmitTaskMsg:
    """Client -> Server: поставить в очередь задание на сбор объявлений."""

    request_id: str
    spec: dict
    options: dict = field(default_factory=dict)
    type: str = "submit_task"


@dataclass
class GetStatusMsg:
    """Client -> Server: запросить статус задания."""

    request_id: str
    task_id: str
    type: str = "get_status"


@dataclass
class StopTaskMsg:
    """Client -> Server: корректно остановить выполнение задания."""

    request_id: str
    task_id: str
    type: str = "stop_task"


@dataclass
class CancelTaskMsg:
    """Client -> Server: отменить задание (удалить из очереди и остановить)."""

    request_id: str
    task_id: str
    type: str = "cancel_task"


@dataclass
class TaskAcceptedMsg:
    """Server -> Client: задание принято в очередь."""

    request_id: str
    task_id: str
    estimated_seconds: int
    queue_position: int
    type: str = "task_accepted"


@dataclass
class StatusMsg:
    """Server -> Client: текущий статус задания."""

    task_id: str
    state: str
    current_site: Optional[str] = None
    progress: Optional[dict] = None
    eta_seconds: Optional[int] = None
    errors: Optional[list] = None
    type: str = "status"


@dataclass
class TaskCompletedMsg:
    """Server -> Client: задание выполнено успешно."""

    task_id: str
    total_ads: int
    type: str = "task_completed"


@dataclass
class TaskFailedMsg:
    """Server -> Client: задание завершилось с ошибкой."""

    task_id: str
    reason: str
    type: str = "task_failed"


@dataclass
class TaskStoppedMsg:
    """Server -> Client: задание остановлено по запросу клиента."""

    task_id: str
    total_ads: int
    type: str = "task_stopped"


@dataclass
class CaptchaRequiredMsg:
    """Server -> Client: на площадке обнаружена CAPTCHA (S-13)."""

    task_id: str
    site: str
    url: str
    type: str = "captcha_required"


@dataclass
class ErrorMsg:
    """Server -> Client: ошибка обработки запроса (валидация, протокол)."""

    request_id: str
    code: str
    message: str
    type: str = "error"


#: Любое сообщение контракта (для аннотаций сигнатур serialize/deserialize).
Message = Union[
    SubmitTaskMsg,
    GetStatusMsg,
    StopTaskMsg,
    CancelTaskMsg,
    TaskAcceptedMsg,
    StatusMsg,
    TaskCompletedMsg,
    TaskFailedMsg,
    TaskStoppedMsg,
    CaptchaRequiredMsg,
    ErrorMsg,
]

#: Таблица «type -> dataclass» — основа явного разбора в deserialize.
MESSAGE_TYPES: dict[str, type] = {
    "submit_task": SubmitTaskMsg,
    "get_status": GetStatusMsg,
    "stop_task": StopTaskMsg,
    "cancel_task": CancelTaskMsg,
    "task_accepted": TaskAcceptedMsg,
    "status": StatusMsg,
    "task_completed": TaskCompletedMsg,
    "task_failed": TaskFailedMsg,
    "task_stopped": TaskStoppedMsg,
    "captcha_required": CaptchaRequiredMsg,
    "error": ErrorMsg,
}


# ---------------------------------------------------------------------------
# Утилиты проверки типов полей
# ---------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    """True, если value — целое (bool не считается целым в протоколе)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require(data: dict, name: str, msg_type: str) -> Any:
    """Возвращает обязательное поле или бросает missing_field (PR-03)."""
    if name not in data or data[name] is None:
        raise InvalidMessageError(f"missing_field: {name} ({msg_type})")
    return data[name]


def _as_str(data: dict, name: str, msg_type: str, *, required: bool = True) -> Any:
    """Поле как строка; при неверном типе — invalid_field (PR-04)."""
    if name not in data:
        if required:
            raise InvalidMessageError(f"missing_field: {name} ({msg_type})")
        return None
    value = data[name]
    if value is None:
        if required:
            raise InvalidMessageError(f"missing_field: {name} ({msg_type})")
        return None
    if not isinstance(value, str):
        raise InvalidMessageError(
            f"invalid_field: {name} ожидалась строка, "
            f"получено {type(value).__name__} ({msg_type})"
        )
    return value


def _as_opt_int(data: dict, name: str, msg_type: str) -> Optional[int]:
    """Необязательное целое поле (None допускается)."""
    value = data.get(name)
    if value is None:
        return None
    if not _is_int(value):
        raise InvalidMessageError(
            f"invalid_field: {name} ожидалось целое, "
            f"получено {type(value).__name__} ({msg_type})"
        )
    return int(value)


def _as_dict(data: dict, name: str, msg_type: str, *, required: bool = True) -> Any:
    """Поле как dict (обязательное или опциональное)."""
    if name not in data:
        if required:
            raise InvalidMessageError(f"missing_field: {name} ({msg_type})")
        return None
    value = data[name]
    if value is None:
        if required:
            raise InvalidMessageError(f"missing_field: {name} ({msg_type})")
        return None
    if not isinstance(value, dict):
        raise InvalidMessageError(
            f"invalid_field: {name} ожидался объект, "
            f"получено {type(value).__name__} ({msg_type})"
        )
    return value


def _as_list(data: dict, name: str, msg_type: str) -> Any:
    """Поле как list (обязательное)."""
    value = _require(data, name, msg_type)
    if not isinstance(value, list):
        raise InvalidMessageError(
            f"invalid_field: {name} ожидался список, "
            f"получено {type(value).__name__} ({msg_type})"
        )
    return value


# ---------------------------------------------------------------------------
# Валидация spec / options (раздел 2.4 промпта задачи №3)
# ---------------------------------------------------------------------------
def validate_spec(spec: Any) -> None:
    """Проверить содержательную корректность спецификации задания.

    Args:
        spec: словарь ``spec`` из :class:`SubmitTaskMsg`.

    Raises:
        InvalidMessageError: с кодом ``invalid_spec`` / ``invalid_mode`` /
            ``invalid_year_range`` / ``invalid_field``.
    """
    if not isinstance(spec, dict):
        raise InvalidMessageError("invalid_spec: ожидался объект")

    mode = spec.get("mode")
    if mode not in VALID_MODES:
        raise InvalidMessageError(
            f"invalid_mode: mode должен быть одним из {list(VALID_MODES)}, "
            f"получено {mode!r}"
        )

    for name in ("manufacturer", "model"):
        value = spec.get(name)
        if not isinstance(value, str) or not value.strip():
            raise InvalidMessageError(
                f"invalid_field: spec.{name} должна быть непустой строкой"
            )

    year_from = spec.get("year_from")
    year_to = spec.get("year_to")
    if not _is_int(year_from) or not _is_int(year_to):
        raise InvalidMessageError(
            "invalid_field: spec.year_from/spec.year_to должны быть целыми"
        )
    if mode == "exact" and year_from != year_to:
        raise InvalidMessageError(
            f"invalid_year_range: для mode=exact год выпуска один "
            f"(year_from={year_from}, year_to={year_to})"
        )
    if mode == "range" and year_from > year_to:
        raise InvalidMessageError(
            f"invalid_year_range: year_from > year_to "
            f"({year_from} > {year_to})"
        )

    generation = spec.get("generation")
    if generation is not None and not isinstance(generation, str):
        raise InvalidMessageError("invalid_field: spec.generation должна быть строкой")

    for name in ("transmissions", "fuels"):
        value = spec.get(name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise InvalidMessageError(
                f"invalid_field: spec.{name} должен быть списком строк"
            )

    vol_from = spec.get("engine_volume_from")
    vol_to = spec.get("engine_volume_to")
    for name, value in (("engine_volume_from", vol_from), ("engine_volume_to", vol_to)):
        if isinstance(value, bool) or (
            value is not None and not isinstance(value, (int, float))
        ):
            raise InvalidMessageError(
                f"invalid_field: spec.{name} должно быть числом"
            )
    if vol_from is not None and vol_to is not None and vol_from > vol_to:
        raise InvalidMessageError(
            f"invalid_year_range: engine_volume_from > engine_volume_to "
            f"({vol_from} > {vol_to})"
        )


def validate_options(options: Any) -> dict:
    """Проверить параметры задания и вернуть их со значениями по умолчанию.

    Args:
        options: словарь ``options`` из :class:`SubmitTaskMsg` (может быть
            пустым — тогда применяются дефолты контракта).

    Returns:
        dict: нормализованные ``{"sites": [...], "max_ads_per_site": int,
        "max_pages_per_site": int}``.

    Raises:
        InvalidMessageError: при недопустимом значении поля.
    """
    if not isinstance(options, dict):
        raise InvalidMessageError("invalid_spec: options ожидался объект")

    sites = options.get("sites")
    if sites is None:
        # По умолчанию — все площадки, кроме sberauto.com (S-15).
        sites = list(DEFAULT_SITES)
    if not isinstance(sites, list) or not all(
        isinstance(site, str) and site in ALLOWED_SITES for site in sites
    ):
        bad = [s for s in sites if s not in ALLOWED_SITES] if isinstance(sites, list) else sites
        raise InvalidMessageError(
            f"invalid_field: options.sites содержит недопустимые площадки: {bad}; "
            f"допустимо {sorted(ALLOWED_SITES)}"
        )

    result: dict[str, Any] = {"sites": list(sites)}
    for name, default in (
        ("max_ads_per_site", DEFAULT_MAX_ADS_PER_SITE),
        ("max_pages_per_site", DEFAULT_MAX_PAGES_PER_SITE),
    ):
        value = options.get(name, default)
        if not _is_int(value) or value <= 0:
            raise InvalidMessageError(
                f"invalid_field: options.{name} должно быть целым > 0"
            )
        result[name] = int(value)
    return result


# ---------------------------------------------------------------------------
# Десериализация конкретных типов (явный разбор каждого поля)
# ---------------------------------------------------------------------------
def _parse_submit_task(data: dict) -> SubmitTaskMsg:
    """Разбор submit_task + внутренняя валидация spec/options (п. 2.4)."""
    msg = SubmitTaskMsg(
        request_id=_as_str(data, "request_id", "submit_task"),
        spec=_as_dict(data, "spec", "submit_task"),
        options=_as_dict(data, "options", "submit_task", required=False) or {},
    )
    validate_spec(msg.spec)
    validate_options(msg.options)
    return msg


def _parse_get_status(data: dict) -> GetStatusMsg:
    return GetStatusMsg(
        request_id=_as_str(data, "request_id", "get_status"),
        task_id=_as_str(data, "task_id", "get_status"),
    )


def _parse_stop_task(data: dict) -> StopTaskMsg:
    return StopTaskMsg(
        request_id=_as_str(data, "request_id", "stop_task"),
        task_id=_as_str(data, "task_id", "stop_task"),
    )


def _parse_cancel_task(data: dict) -> CancelTaskMsg:
    return CancelTaskMsg(
        request_id=_as_str(data, "request_id", "cancel_task"),
        task_id=_as_str(data, "task_id", "cancel_task"),
    )


def _parse_task_accepted(data: dict) -> TaskAcceptedMsg:
    """Разбор task_accepted (все поля обязательны, целые — валидируются)."""
    estimated = data.get("estimated_seconds")
    if estimated is None:
        raise InvalidMessageError("missing_field: estimated_seconds (task_accepted)")
    if not _is_int(estimated):
        raise InvalidMessageError(
            f"invalid_field: estimated_seconds ожидалось целое, "
            f"получено {type(estimated).__name__} (task_accepted)"
        )
    position = data.get("queue_position")
    if position is None:
        raise InvalidMessageError("missing_field: queue_position (task_accepted)")
    if not _is_int(position):
        raise InvalidMessageError(
            f"invalid_field: queue_position ожидалось целое, "
            f"получено {type(position).__name__} (task_accepted)"
        )
    return TaskAcceptedMsg(
        request_id=_as_str(data, "request_id", "task_accepted"),
        task_id=_as_str(data, "task_id", "task_accepted"),
        estimated_seconds=int(estimated),
        queue_position=int(position),
    )


def _require_int(data: dict, name: str, msg_type: str) -> int:
    """Обязательное целочисленное поле (PR-03/PR-04)."""
    value = data.get(name)
    if value is None:
        raise InvalidMessageError(f"missing_field: {name} ({msg_type})")
    if not _is_int(value):
        raise InvalidMessageError(
            f"invalid_field: {name} ожидалось целое, "
            f"получено {type(value).__name__} ({msg_type})"
        )
    return int(value)


def _parse_status(data: dict) -> StatusMsg:
    task_id = _as_str(data, "task_id", "status")
    state = _as_str(data, "state", "status")
    if state not in TASK_STATES:
        raise InvalidMessageError(
            f"invalid_field: state должен быть одним из "
            f"{sorted(TASK_STATES)}, получено {state!r} (status)"
        )
    current_site = _as_str(data, "current_site", "status", required=False)
    progress = _as_dict(data, "progress", "status", required=False)
    eta_seconds = _as_opt_int(data, "eta_seconds", "status")
    errors: Optional[list] = None
    if "errors" in data and data["errors"] is not None:
        errors = _as_list(data, "errors", "status")
    return StatusMsg(
        task_id=task_id,
        state=state,
        current_site=current_site,
        progress=progress,
        eta_seconds=eta_seconds,
        errors=errors,
    )


def _parse_task_completed(data: dict) -> TaskCompletedMsg:
    """Разбор task_completed."""
    return TaskCompletedMsg(
        task_id=_as_str(data, "task_id", "task_completed"),
        total_ads=_require_int(data, "total_ads", "task_completed"),
    )


def _parse_task_failed(data: dict) -> TaskFailedMsg:
    return TaskFailedMsg(
        task_id=_as_str(data, "task_id", "task_failed"),
        reason=_as_str(data, "reason", "task_failed"),
    )


def _parse_task_stopped(data: dict) -> TaskStoppedMsg:
    """Разбор task_stopped."""
    return TaskStoppedMsg(
        task_id=_as_str(data, "task_id", "task_stopped"),
        total_ads=_require_int(data, "total_ads", "task_stopped"),
    )


def _parse_captcha_required(data: dict) -> CaptchaRequiredMsg:
    return CaptchaRequiredMsg(
        task_id=_as_str(data, "task_id", "captcha_required"),
        site=_as_str(data, "site", "captcha_required"),
        url=_as_str(data, "url", "captcha_required"),
    )


def _parse_error(data: dict) -> ErrorMsg:
    return ErrorMsg(
        request_id=_as_str(data, "request_id", "error"),
        code=_as_str(data, "code", "error"),
        message=_as_str(data, "message", "error"),
    )


_PARSERS = {
    "submit_task": _parse_submit_task,
    "get_status": _parse_get_status,
    "stop_task": _parse_stop_task,
    "cancel_task": _parse_cancel_task,
    "task_accepted": _parse_task_accepted,
    "status": _parse_status,
    "task_completed": _parse_task_completed,
    "task_failed": _parse_task_failed,
    "task_stopped": _parse_task_stopped,
    "captcha_required": _parse_captcha_required,
    "error": _parse_error,
}


# ---------------------------------------------------------------------------
# Публичные функции контракта (раздел 2.2)
# ---------------------------------------------------------------------------
def deserialize(line: str) -> Message:
    """Десериализовать одну JSON-строку в dataclass сообщения.

    Args:
        line: JSON-строка (без завершающего ``\\n``).

    Returns:
        Message: экземпляр соответствующего dataclass'а.

    Raises:
        InvalidMessageError: не-JSON (``invalid_json``), неизвестный ``type``
            (``unknown_type``), отсутствующее обязательное поле
            (``missing_field``), неверный тип/значение поля
            (``invalid_field``) или ошибка содержательной валидации spec/options.
    """
    if not isinstance(line, str):
        raise InvalidMessageError("invalid_json: ожидалась строка")
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidMessageError(f"invalid_json: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidMessageError(
            f"invalid_json: ожидался объект, получено {type(data).__name__}"
        )
    msg_type = data.get("type")
    if not isinstance(msg_type, str) or msg_type not in MESSAGE_TYPES:
        raise InvalidMessageError(f"unknown_type: {msg_type!r}")
    return _PARSERS[msg_type](data)


def serialize(msg: Message) -> str:
    """Сериализовать dataclass сообщения в JSON-строку (без ``\\n``).

    UTF-8, нe-ASCII символы сохраняются как есть (PR-08, ensure_ascii=False).

    Raises:
        ValueError: передан объект вне контракта или без поля ``type`` (PR-06).
    """
    if not (is_dataclass_instance(msg)):
        raise ValueError(f"serialize: ожидался dataclass, получено {type(msg).__name__}")
    data = asdict(msg)
    msg_type = data.get("type")
    if msg_type not in MESSAGE_TYPES:
        raise ValueError(f"serialize: неизвестный или отсутствующий type: {msg_type!r}")
    return json.dumps(data, ensure_ascii=False)


def is_dataclass_instance(obj: Any) -> bool:
    """True, если obj — экземпляр dataclass'а из контракта MESSAGE_TYPES."""
    import dataclasses

    return dataclasses.is_dataclass(obj) and not isinstance(obj, type)


def validate_message(msg: Message) -> None:
    """Проверить обязательные поля готового сообщения (PR-03/PR-04).

    Для :class:`SubmitTaskMsg` дополнительно выполняет содержательную
    валидацию ``spec`` и ``options`` (раздел 2.4).

    Raises:
        InvalidMessageError: при нарушении контракта.
        ValueError: если msg вне контракта (PR-06).
    """
    if not is_dataclass_instance(msg):
        raise ValueError(f"validate_message: ожидался dataclass, получено {type(msg).__name__}")
    if getattr(msg, "type", None) not in MESSAGE_TYPES:
        raise ValueError(f"validate_message: неизвестный type: {getattr(msg, 'type', None)!r}")

    problems: list[str] = []
    if isinstance(msg, SubmitTaskMsg):
        if not isinstance(msg.request_id, str) or not msg.request_id:
            problems.append("missing_field: request_id (submit_task)")
        if not isinstance(msg.spec, dict):
            problems.append("missing_field: spec (submit_task)")
        if not isinstance(msg.options, dict):
            problems.append("invalid_field: options (submit_task)")
        if problems:
            raise InvalidMessageError("; ".join(problems))
        validate_spec(msg.spec)
        validate_options(msg.options)
        return

    if isinstance(msg, (GetStatusMsg, StopTaskMsg, CancelTaskMsg)):
        checks = [("request_id", str(msg.request_id)), ("task_id", str(msg.task_id))]
    elif isinstance(msg, TaskAcceptedMsg):
        checks = [
            ("request_id", str(msg.request_id)),
            ("task_id", str(msg.task_id)),
            ("estimated_seconds", msg.estimated_seconds),
            ("queue_position", msg.queue_position),
        ]
        for value in (msg.estimated_seconds, msg.queue_position):
            if not _is_int(value):
                problems.append(f"invalid_field: целое ожидалось ({msg.type})")
                break
    elif isinstance(msg, StatusMsg):
        checks = [("task_id", str(msg.task_id)), ("state", str(msg.state))]
        if msg.state not in TASK_STATES:
            problems.append(f"invalid_field: state {msg.state!r} ({msg.type})")
    elif isinstance(msg, (TaskCompletedMsg, TaskStoppedMsg)):
        checks = [("task_id", str(msg.task_id)), ("total_ads", msg.total_ads)]
        if not _is_int(msg.total_ads):
            problems.append(f"invalid_field: total_ads ожидалось целое ({msg.type})")
    elif isinstance(msg, TaskFailedMsg):
        checks = [("task_id", str(msg.task_id)), ("reason", str(msg.reason))]
    elif isinstance(msg, CaptchaRequiredMsg):
        checks = [
            ("task_id", str(msg.task_id)),
            ("site", str(msg.site)),
            ("url", str(msg.url)),
        ]
    elif isinstance(msg, ErrorMsg):
        checks = [
            ("request_id", str(msg.request_id)),
            ("code", str(msg.code)),
            ("message", str(msg.message)),
        ]
    else:  # pragma: no cover - ветка защищена проверкой type выше
        raise ValueError(f"validate_message: неподдерживаемый класс {type(msg).__name__}")

    for name, value in checks:
        if value is None or (isinstance(value, str) and not value):
            problems.append(f"missing_field: {name} ({msg.type})")
    if problems:
        raise InvalidMessageError("; ".join(problems))
