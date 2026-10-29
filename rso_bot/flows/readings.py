"""Meter-reading flow for the MAX bot.

Update parsing and routing stay in :mod:`bot`.  This module owns the meter
selection, value entry, validation, confirmation and persistence transitions.
Every collaborator is supplied explicitly so importing the flow never starts
the bot or creates a circular dependency.

Meters come from the 1C import.  Each meter carries an ordered list of
``zones`` (one entry per scale/tariff-zone pair, already labelled by the
database layer).  Entered values are kept in ``state["meter_values"]`` in the
same order, so the index of the pair being asked is ``len(meter_values)``.
Each pair may carry ``previous``: the larger of the last non-rejected value
saved by the bot and the reading imported from 1C.  A value below it is
refused; the database re-checks it together with meter ownership on save.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from rso_bot.reading_values import (
    READING_MAX_FRACTION_DIGITS,
    READING_MAX_INTEGER_DIGITS,
    SAVE_METER_CHANGED,
    SAVE_OK,
    SAVE_TOO_LOW,
    format_reading,
    reading_decimal,
    reading_error,
)

State = dict[str, Any]
Button = dict[str, Any]
Record = Mapping[str, Any]


@dataclass(frozen=True)
class ReadingDependencies:
    """Database, session, transport and continuation dependencies."""

    get_meters: Callable[[str], Sequence[Record]]
    # save_reading(chat_id, ls, meter_id, values) -> {"status": SAVE_*, ...}
    save_reading: Callable[..., Mapping[str, Any]]
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


_READING_RE = re.compile(r"([+-]?)(\d+)(?:[.,](\d+))?", re.ASCII)
_READING_ERRORS = {
    "negative": "Введите неотрицательное числовое значение.",
    "fraction": f"Допускается не более {READING_MAX_FRACTION_DIGITS} знаков после запятой.",
    "integer": f"Допускается не более {READING_MAX_INTEGER_DIGITS} цифр до запятой.",
}


def parse_reading(chat_id: int, text: str, deps: ReadingDependencies) -> str | None:
    """Parse a non-negative decimal reading and return its normalised text.

    Only digits with one optional ``.``/``,`` separator are accepted, so
    exponents, ``inf`` and ``nan`` never pass.  ``Decimal`` keeps long values
    exact: ``"00123,50"`` becomes ``"123.5"``.  Digit limits are shared with
    the ``Reading`` field of the 1C import (:mod:`rso_bot.reading_values`).
    """
    match = _READING_RE.fullmatch(text.strip())
    if match is None:
        deps.send_message(chat_id, "Введите числовое значение.")
        return None
    sign, integer, fraction = match.groups()
    value = Decimal(f"{sign}{integer}.{fraction}" if fraction else f"{sign}{integer}")
    error = reading_error(value)  # "-0" keeps its sign and is rejected as negative
    if error is not None:
        deps.send_message(chat_id, _READING_ERRORS.get(error, "Введите числовое значение."))
        return None
    return format_reading(value)


def _previous_hint(zone: Record) -> str | None:
    """Normalised previous reading of a pair, or None when it is absent or zero."""
    previous = reading_decimal(zone.get("previous"))
    if previous is None or previous <= 0:
        return None
    return format_reading(previous)


def meter_title(meter: Record) -> str:
    """Return "Ресурс №номер (тип из 1С)"; the brackets are omitted if empty."""
    title = f"{meter['resource_type']} №{meter['meter_number']}"
    meter_type = (meter.get("meter_type") or "").strip()
    return f"{title} ({meter_type})" if meter_type else title


def _entered_values(state: State) -> list[str]:
    return list(state.get("meter_values") or [])


def _apply_previous(meter: dict, previous: Mapping[tuple[str, str], str | None]) -> None:
    """Replace session ``previous`` values with the ones re-read on save."""
    for zone in meter["zones"]:
        key = (zone["scale_one_c_id"], zone["zone_one_c_id"])
        if key in previous:
            zone["previous"] = previous[key]


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
    hint = _previous_hint(zones[index])
    if hint is not None:
        prompt = f"{prompt}\nПредыдущее показание: {hint}"
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
            "scale_id": zone["scale_one_c_id"],
            "zone_id": zone["zone_one_c_id"],
            "value": value,
        }
        for zone, value in zip(zones, entered)
    ]
    try:
        result = deps.save_reading(
            chat_id,
            state["ls"],
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

    status = result.get("status")
    if status == SAVE_METER_CHANGED:
        # The meter moved to another account or its scales/zones changed after
        # it was selected: nothing was saved, so reload the meter list.
        account = state["ls"]
        deps.clear_flow(state)
        deps.touch(state)
        deps.send_message(
            chat_id, "Данные по счётчику обновились, введите показания заново."
        )
        deps.show_meter_select(chat_id, account)
        return
    if status == SAVE_TOO_LOW:
        # A reading saved concurrently raised the previous value. Remember the
        # fresh previous values and restart input for this meter.
        _apply_previous(meter, result.get("previous") or {})
        label = next(
            (
                zone["label"] for zone in zones
                if (zone["scale_one_c_id"], zone["zone_one_c_id"])
                == (result.get("scale_id"), result.get("zone_id"))
            ),
            None,
        )
        subject = f"Показание «{label}»" if label else "Показание"
        deps.send_message(
            chat_id,
            f"{subject} не может быть меньше предыдущего "
            f"({result.get('previous_value')}). Введите показания заново.",
        )
        deps.reset_meter_input(state)
        deps.touch(state)
        deps.ask_meter_value(chat_id)
        return
    if status != SAVE_OK:
        deps.logger.error("Неизвестный результат сохранения показаний")
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
    if len(entered) >= len(meter["zones"]):
        deps.confirm_meter_reading(chat_id)
        return
    zone = meter["zones"][len(entered)]
    previous = reading_decimal(zone.get("previous"))
    if previous is not None and Decimal(value) < previous:
        # Equal values are allowed; the state stays on the same pair.
        deps.send_message(
            chat_id,
            f"Показание не может быть меньше предыдущего ({format_reading(previous)}). "
            "Введите значение ещё раз:",
        )
        return

    entered.append(value)
    state["meter_values"] = entered
    deps.touch(state)
    if len(entered) < len(meter["zones"]):
        deps.ask_meter_value(chat_id)
    else:
        deps.confirm_meter_reading(chat_id)
