"""Meter-reading flow for the MAX bot.

Update parsing and routing stay in :mod:`bot`.  This module owns the meter
selection, value entry, validation, confirmation and persistence transitions.
Every collaborator is supplied explicitly so importing the flow never starts
the bot or creates a circular dependency.

Meters come from the 1C import.  Each meter carries an ordered list of
``zones`` (one entry per scale/tariff-zone pair, already labelled by the
database layer).  Entered values are kept in ``state["meter_values"]`` in the
same order, so the index of the pair being asked is ``len(meter_values)``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

State = dict[str, Any]
Button = dict[str, Any]
Record = Mapping[str, Any]


@dataclass(frozen=True)
class ReadingDependencies:
    """Database, session, transport and continuation dependencies."""

    get_meters: Callable[[str], Sequence[Record]]
    add_reading: Callable[..., Any]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    reset_meter_input: Callable[[State], None]
    clear_flow: Callable[[State], None]
    get_saved_ls: Callable[[int], str | None]
    request_ls: Callable[[int, str], None]
    # Calls through entry-point wrappers preserve runtime monkeypatch seams.
    parse_input: Callable[[int, str], str | None]
    show_meter_select: Callable[[int, str], None]
    ask_meter_value: Callable[[int], None]
    confirm_meter_reading: Callable[[int], None]
    make_callback: Callable[[str, str], Button]
    send_message: Callable[[int, str], Any]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    logger: logging.Logger
    integration_enabled: bool
    meter_select_state: str
    waiting_value_state: str
    confirm_state: str


READING_MAX_INTEGER_DIGITS = 15
READING_MAX_FRACTION_DIGITS = 6
_READING_RE = re.compile(r"([+-]?)(\d+)(?:[.,](\d+))?", re.ASCII)


def parse_reading(chat_id: int, text: str, deps: ReadingDependencies) -> str | None:
    """Parse a non-negative decimal reading and return its normalised text.

    Only digits with one optional ``.``/``,`` separator are accepted, so
    exponents, ``inf`` and ``nan`` never pass.  ``Decimal`` keeps long values
    exact: ``"00123,50"`` becomes ``"123.5"``.
    """
    match = _READING_RE.fullmatch(text.strip())
    if match is None:
        deps.send_message(chat_id, "Введите числовое значение.")
        return None
    sign, integer, fraction = match.groups()
    if sign == "-":
        deps.send_message(chat_id, "Введите неотрицательное числовое значение.")
        return None
    if fraction and len(fraction) > READING_MAX_FRACTION_DIGITS:
        deps.send_message(
            chat_id,
            f"Допускается не более {READING_MAX_FRACTION_DIGITS} знаков после запятой.",
        )
        return None
    if len(integer.lstrip("0")) > READING_MAX_INTEGER_DIGITS:
        deps.send_message(
            chat_id,
            f"Допускается не более {READING_MAX_INTEGER_DIGITS} цифр до запятой.",
        )
        return None
    value = Decimal(f"{integer}.{fraction or '0'}").normalize()
    return format(value, "f")


def meter_title(meter: Record) -> str:
    """Return "Ресурс №номер (тип из 1С)"; the brackets are omitted if empty."""
    title = f"{meter['resource_type']} №{meter['meter_number']}"
    meter_type = (meter.get("meter_type") or "").strip()
    return f"{title} ({meter_type})" if meter_type else title


def _entered_values(state: State) -> list[str]:
    return list(state.get("meter_values") or [])


def start(chat_id: int, deps: ReadingDependencies) -> None:
    """Start readings for a saved account, or defer until account binding."""
    account = deps.get_saved_ls(chat_id)
    if not account:
        deps.request_ls(chat_id, "pokazaniya")
        return
    deps.show_meter_select(chat_id, account)


def show_meter_select(chat_id: int, account: str, deps: ReadingDependencies) -> None:
    """Load account meters and present the meter selection keyboard."""
    # A meter without scale/zone pairs has nothing to enter, so it is not
    # offered at all; the database layer already omits such meters.
    meter_list = [
        dict(meter) for meter in deps.get_meters(account) if meter.get("zones")
    ]
    if not meter_list:
        deps.send_message(chat_id, "По вашему счёту счётчики не найдены.")
        deps.send_main_menu(chat_id)
        return

    state = deps.get_state(chat_id)
    state["state"] = deps.meter_select_state
    state["ls"] = account
    state["flow_ls"] = account
    state["meters"] = meter_list
    deps.touch(state)

    rows = [
        [deps.make_callback(meter_title(meter), f"meter:{index}")]
        for index, meter in enumerate(meter_list)
    ]
    rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
    deps.send_buttons(chat_id, "Выберите счётчик:", rows)


def ask_meter_value(chat_id: int, deps: ReadingDependencies) -> None:
    """Prompt for the next scale/zone pair of the selected meter."""
    state = deps.get_state(chat_id)
    meter = state["meters"][state["meter_idx"]]
    zones = meter["zones"]
    index = len(_entered_values(state))
    if index >= len(zones):
        deps.confirm_meter_reading(chat_id)
        return

    prompt = f"{zones[index]['label']}:"
    if index == 0:
        prompt = f"{meter_title(meter)}\n{prompt}"
    deps.send_message(chat_id, prompt)


def confirm_meter_reading(chat_id: int, deps: ReadingDependencies) -> None:
    """Show the confirmation keyboard for the values in session state."""
    state = deps.get_state(chat_id)
    meter = state["meters"][state["meter_idx"]]
    lines = [meter_title(meter)] + [
        f"{zone['label']}: {value}"
        for zone, value in zip(meter["zones"], _entered_values(state))
    ]

    state["state"] = deps.confirm_state
    deps.touch(state)
    deps.send_buttons(
        chat_id,
        "Проверьте показания:\n" + "\n".join(lines),
        [
            [deps.make_callback("✅ Подтвердить", "meter_confirm")],
            [deps.make_callback("✏️ Скорректировать", "meter_retry")],
        ],
    )


def select_meter(
    chat_id: int,
    state: State,
    argument: str,
    deps: ReadingDependencies,
) -> None:
    """Select a meter and reset value entry to its first scale/zone pair."""
    meter_index = int(argument)
    meters = state.get("meters")
    if not isinstance(meters, Sequence) or not 0 <= meter_index < len(meters):
        raise ValueError("meter index is outside the available range")
    state["meter_idx"] = meter_index
    deps.reset_meter_input(state)
    deps.touch(state)
    deps.ask_meter_value(chat_id)


def confirm(chat_id: int, state: State, deps: ReadingDependencies) -> None:
    """Persist confirmed values and return to the account's meter list."""
    if state.get("state") != deps.confirm_state:
        return

    meter = state["meters"][state["meter_idx"]]
    zones = meter["zones"]
    entered = _entered_values(state)
    if len(entered) != len(zones):
        # Never persist a partial set of values: start this meter over.
        retry(chat_id, state, deps)
        return

    values = [
        {
            "scale_one_c_id": zone["scale_one_c_id"],
            "scale_name": zone.get("scale_name"),
            "zone_one_c_id": zone["zone_one_c_id"],
            "zone_name": zone.get("zone_name"),
            "value": value,
        }
        for zone, value in zip(zones, entered)
    ]
    try:
        deps.add_reading(
            chat_id,
            state["ls"],
            meter["resource_type"],
            meter["meter_number"],
            meter["meter_one_c_id"],
            values,
        )
    except Exception:  # noqa: BLE001 - persistence failures must keep the flow retryable
        # The exception raised by the persistence layer can contain account and
        # meter values. Keep the operational signal while deliberately omitting
        # both exception details and reading context from the log.
        deps.logger.error("Не удалось сохранить показания счётчика")
        deps.send_message(
            chat_id,
            "Не удалось сохранить показания. Попробуйте подтвердить ещё раз позже.",
        )
        return
    if deps.integration_enabled:
        deps.logger.info(
            "Показания сохранены в очередь 1С: ЛС=%s  счётчик=%s  chat_id=%s",
            state["ls"],
            meter["meter_number"],
            chat_id,
        )
    else:
        deps.logger.info(
            "Показания приняты: ЛС=%s  счётчик=%s  chat_id=%s",
            state["ls"],
            meter["meter_number"],
            chat_id,
        )

    account = state["ls"]
    deps.clear_flow(state)
    deps.touch(state)
    if deps.integration_enabled:
        deps.send_message(
            chat_id,
            f"✅ Показания по счётчику {meter['resource_type']} "
            f"№{meter['meter_number']} сохранены и ожидают обработки в 1С.",
        )
    else:
        deps.send_message(
            chat_id,
            f"✅ Показания по счётчику {meter['resource_type']} "
            f"№{meter['meter_number']} приняты!",
        )
    deps.show_meter_select(chat_id, account)


def retry(chat_id: int, state: State, deps: ReadingDependencies) -> None:
    """Reset the current meter's values and ask for the first pair again."""
    if state.get("state") != deps.confirm_state:
        return
    deps.reset_meter_input(state)
    deps.touch(state)
    deps.ask_meter_value(chat_id)


def on_value(
    chat_id: int,
    state: State,
    text: str,
    deps: ReadingDependencies,
) -> None:
    """Accept the value for the current scale/zone pair and advance the flow."""
    meter = state["meters"][state["meter_idx"]]
    value = deps.parse_input(chat_id, text)
    if value is None:
        return

    entered = _entered_values(state)
    entered.append(value)
    state["meter_values"] = entered
    deps.touch(state)
    if len(entered) < len(meter["zones"]):
        deps.ask_meter_value(chat_id)
    else:
        deps.confirm_meter_reading(chat_id)
