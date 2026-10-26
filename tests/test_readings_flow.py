from __future__ import annotations

import json
import logging
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

import pytest

import bot
import database as db
from rso_bot.flows import readings

ONE_ZONE = (("Основная", ("Сутки",)),)
TWO_ZONES = (("Основная", ("День", "Ночь")),)
THREE_ZONES = (("Основная", ("Пик", "Полупик", "Ночь")),)
TWO_SCALES = (("Актив", ("День", "Ночь")), ("Реактив", ("День", "Ночь")))


def meter(scales=ONE_ZONE, *, meter_type: str | None = "Однотарифный") -> dict:
    """Meter in the shape returned by ``db.get_1c_meters_with_zones``."""
    return {
        "meter_one_c_id": "METER-GUID",
        "resource_type": "Электроэнергия",
        "meter_number": "E-1",
        "meter_type": meter_type,
        "zones": [
            {
                "scale_one_c_id": f"S{scale_index}",
                "scale_name": scale,
                "zone_one_c_id": f"Z{scale_index}{zone_index}",
                "zone_name": zone,
                "label": f"{scale}, {zone}",
            }
            for scale_index, (scale, zones) in enumerate(scales)
            for zone_index, zone in enumerate(zones)
        ],
    }


def make_deps(**overrides) -> readings.ReadingDependencies:
    states: dict[int, dict] = {}

    def get_state(chat_id: int) -> dict:
        return states.setdefault(chat_id, {})

    defaults = {
        "get_meters": MagicMock(return_value=[]),
        "add_reading": MagicMock(),
        "get_state": get_state,
        "touch": MagicMock(side_effect=lambda state: state),
        "reset_meter_input": MagicMock(),
        "clear_flow": MagicMock(),
        "get_saved_ls": MagicMock(return_value=None),
        "request_ls": MagicMock(),
        "parse_input": MagicMock(),
        "show_meter_select": MagicMock(),
        "ask_meter_value": MagicMock(),
        "confirm_meter_reading": MagicMock(),
        "make_callback": lambda label, payload: {"text": label, "payload": payload},
        "send_message": MagicMock(),
        "send_buttons": MagicMock(),
        "send_main_menu": MagicMock(),
        "logger": logging.getLogger("test.readings"),
        "integration_enabled": False,
        "meter_select_state": "meter_select",
        "waiting_value_state": "waiting_value",
        "confirm_state": "confirm",
    }
    defaults.update(overrides)
    return readings.ReadingDependencies(**defaults)


def wired_deps(**overrides) -> readings.ReadingDependencies:
    """Dependencies whose flow continuations call the real flow functions."""
    holder: dict[str, readings.ReadingDependencies] = {}

    def reset(state: dict) -> None:
        state.pop("meter_values", None)
        state["state"] = "waiting_value"

    deps = make_deps(
        reset_meter_input=reset,
        parse_input=lambda chat_id, text: readings.parse_reading(
            chat_id, text, holder["deps"]
        ),
        ask_meter_value=lambda chat_id: readings.ask_meter_value(
            chat_id, holder["deps"]
        ),
        confirm_meter_reading=lambda chat_id: readings.confirm_meter_reading(
            chat_id, holder["deps"]
        ),
        **overrides,
    )
    holder["deps"] = deps
    return deps


def selected(deps, scales=ONE_ZONE, **meter_kwargs) -> dict:
    state = deps.get_state(7)
    state.update(
        {"state": "meter_select", "ls": "LS", "meters": [meter(scales, **meter_kwargs)]}
    )
    readings.select_meter(7, state, "0", deps)
    return state


def prompts(deps) -> list[str]:
    return [call.args[1] for call in deps.send_message.call_args_list]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.5", "12.5"),
        ("12,5", "12.5"),
        (" 12.5 ", "12.5"),
        ("0", "0"),
        ("0,000", "0"),
        ("00123,50", "123.5"),
        ("100", "100"),
        ("+7", "7"),
        ("123456789012345", "123456789012345"),
        ("000000000000123456789012345", "123456789012345"),
        ("1,123456", "1.123456"),
    ],
)
def test_parse_reading_accepts_non_negative_decimals_exactly(
    raw: str, expected: str
) -> None:
    deps = make_deps()
    assert readings.parse_reading(7, raw, deps) == expected
    deps.send_message.assert_not_called()


@pytest.mark.parametrize("raw", ["-1", "-0,5"])
def test_parse_reading_rejects_negative_values(raw: str) -> None:
    deps = make_deps()
    assert readings.parse_reading(7, raw, deps) is None
    deps.send_message.assert_called_once_with(
        7, "Введите неотрицательное числовое значение."
    )


@pytest.mark.parametrize(
    "raw",
    ["нет", "nan", "inf", "+inf", "-inf", "1e5", "1,2,3", "1.", ",5", "1 000", "", "١٢٣"],
)
def test_parse_reading_reports_non_number(raw: str) -> None:
    deps = make_deps()
    assert readings.parse_reading(7, raw, deps) is None
    deps.send_message.assert_called_once_with(7, "Введите числовое значение.")


def test_parse_reading_limits_fraction_and_integer_digits() -> None:
    deps = make_deps()
    assert readings.parse_reading(7, "1,1234567", deps) is None
    assert readings.parse_reading(7, "1234567890123456", deps) is None
    assert [call.args[1] for call in deps.send_message.call_args_list] == [
        "Допускается не более 6 знаков после запятой.",
        "Допускается не более 15 цифр до запятой.",
    ]


def test_start_uses_saved_account_or_requests_it() -> None:
    missing = make_deps()
    readings.start(7, missing)
    missing.request_ls.assert_called_once_with(7, "pokazaniya")

    saved = replace(missing, get_saved_ls=MagicMock(return_value="LS-1"))
    readings.start(8, saved)
    saved.show_meter_select.assert_called_once_with(8, "LS-1")


def test_empty_meter_list_returns_to_menu() -> None:
    deps = make_deps()
    readings.show_meter_select(7, "LS", deps)
    deps.send_message.assert_called_once_with(7, "По вашему счёту счётчики не найдены.")
    deps.send_main_menu.assert_called_once_with(7)


def test_meter_without_zones_is_not_offered() -> None:
    empty = meter(())
    deps = make_deps(get_meters=MagicMock(return_value=[empty]))
    readings.show_meter_select(7, "LS", deps)
    deps.send_message.assert_called_once_with(7, "По вашему счёту счётчики не найдены.")
    deps.send_buttons.assert_not_called()

    deps = make_deps(get_meters=MagicMock(return_value=[empty, meter(TWO_ZONES)]))
    readings.show_meter_select(7, "LS", deps)
    assert deps.get_state(7)["meters"] == [meter(TWO_ZONES)]


def test_meter_list_labels_use_1c_differentiation_name() -> None:
    deps = make_deps(
        get_meters=MagicMock(
            return_value=[
                meter(TWO_ZONES, meter_type="Двухтарифный"),
                meter(THREE_ZONES, meter_type="Трёхтарифный"),
                meter(meter_type=None),
                meter(meter_type="  "),
            ]
        )
    )
    readings.show_meter_select(7, "LS", deps)
    state = deps.get_state(7)
    assert state["state"] == "meter_select"
    assert (state["ls"], state["flow_ls"]) == ("LS", "LS")
    rows = deps.send_buttons.call_args.args[2]
    assert [row[0]["text"] for row in rows] == [
        "Электроэнергия №E-1 (Двухтарифный)",
        "Электроэнергия №E-1 (Трёхтарифный)",
        "Электроэнергия №E-1",
        "Электроэнергия №E-1",
        "🏠 Главное меню",
    ]
    assert [row[0]["payload"] for row in rows] == [
        "meter:0", "meter:1", "meter:2", "meter:3", "main_menu"
    ]


@pytest.mark.parametrize(
    ("scales", "meter_type", "labels"),
    [
        (ONE_ZONE, "Однотарифный", ["Основная, Сутки"]),
        (TWO_ZONES, "Двухтарифный", ["Основная, День", "Основная, Ночь"]),
        (
            THREE_ZONES,
            "Трёхтарифный",
            ["Основная, Пик", "Основная, Полупик", "Основная, Ночь"],
        ),
        (
            TWO_SCALES,
            "Двухтарифный",
            ["Актив, День", "Актив, Ночь", "Реактив, День", "Реактив, Ночь"],
        ),
    ],
    ids=["1x1", "1x2", "1x3", "2x2"],
)
def test_values_are_requested_per_scale_zone_pair_then_confirmed(
    scales, meter_type: str, labels: list[str]
) -> None:
    deps = wired_deps()
    state = selected(deps, scales, meter_type=meter_type)
    header = f"Электроэнергия №E-1 ({meter_type})"

    entered = [str(100 + index) for index in range(len(labels))]
    for text in entered:
        readings.on_value(7, state, text, deps)

    assert prompts(deps) == [f"{header}\n{labels[0]}:"] + [
        f"{label}:" for label in labels[1:]
    ]
    assert state["state"] == "confirm"
    assert state["meter_values"] == entered
    text, rows = deps.send_buttons.call_args.args[1:]
    assert text == "Проверьте показания:\n" + "\n".join(
        [header] + [f"{label}: {value}" for label, value in zip(labels, entered)]
    )
    assert [row[0]["text"] for row in rows] == ["✅ Подтвердить", "✏️ Скорректировать"]
    assert [row[0]["payload"] for row in rows] == ["meter_confirm", "meter_retry"]
    for prompt in prompts(deps):
        assert "Текущ" not in prompt and "Начальное" not in prompt


@pytest.mark.parametrize("integration_enabled", [False, True])
def test_any_non_negative_value_is_accepted_without_comparison(
    integration_enabled: bool,
) -> None:
    deps = wired_deps(integration_enabled=integration_enabled)
    state = selected(deps, TWO_ZONES)
    readings.on_value(7, state, "1000", deps)
    readings.on_value(7, state, "0", deps)
    assert state["meter_values"] == ["1000", "0"]
    assert state["state"] == "confirm"


@pytest.mark.parametrize("raw", ["-1", "nan", "inf", "abc"])
def test_invalid_values_never_mutate_or_confirm(raw: str) -> None:
    deps = wired_deps()
    state = selected(deps, TWO_ZONES)
    readings.on_value(7, state, "5", deps)
    deps.send_message.reset_mock()
    before = dict(state, meter_values=list(state["meter_values"]))

    readings.on_value(7, state, raw, deps)

    assert state == before
    deps.send_buttons.assert_not_called()
    deps.add_reading.assert_not_called()
    assert len(deps.send_message.call_args_list) == 1


def test_retry_restarts_input_from_first_pair() -> None:
    deps = wired_deps()
    state = selected(deps, TWO_SCALES, meter_type="Двухтарифный")
    for text in ("1", "2", "3", "4"):
        readings.on_value(7, state, text, deps)
    deps.send_message.reset_mock()

    readings.retry(7, state, deps)

    assert state["state"] == "waiting_value"
    assert "meter_values" not in state
    assert prompts(deps) == ["Электроэнергия №E-1 (Двухтарифный)\nАктив, День:"]
    readings.on_value(7, state, "9", deps)
    assert state["meter_values"] == ["9"]
    assert prompts(deps)[-1] == "Актив, Ночь:"


def test_retry_only_resets_confirmation_state() -> None:
    deps = make_deps()
    readings.retry(7, {"state": "waiting_value"}, deps)
    deps.reset_meter_input.assert_not_called()

    state = {"state": "confirm"}
    readings.retry(7, state, deps)
    deps.reset_meter_input.assert_called_once_with(state)
    deps.ask_meter_value.assert_called_once_with(7)


@pytest.mark.parametrize("argument", ["x", "1", "-1"])
def test_invalid_meter_selection_does_not_mutate_state(argument) -> None:
    deps = make_deps(
        reset_meter_input=MagicMock(
            side_effect=lambda state: state.update(state="waiting_value")
        )
    )
    state = {"meters": [meter()]}
    before = state.copy()
    with pytest.raises(ValueError):
        readings.select_meter(7, state, argument, deps)
    assert state == before
    deps.reset_meter_input.assert_not_called()
    deps.ask_meter_value.assert_not_called()


@pytest.mark.parametrize("argument", ["x", "1", "-1"])
def test_invalid_meter_callback_returns_to_menu_without_invalid_state(argument) -> None:
    chat_id = 777
    bot.user_states[chat_id] = {
        "state": bot.S.METER_SELECT,
        "meters": [meter()],
        "ls": "100001",
        "flow_ls": "100001",
    }
    update = {
        "callback": {
            "callback_id": "callback-invalid-meter",
            "payload": f"meter:{argument}",
        },
        "message": {"recipient": {"chat_id": chat_id}},
    }
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "_is_authenticated", return_value=True),
        patch.object(bot, "_bound_account_numbers", return_value={"100001"}),
        patch.object(bot, "send_main_menu") as menu,
    ):
        bot.handle_callback(update)
    menu.assert_called_once_with(chat_id)
    assert "meter_idx" not in bot.user_states[chat_id]
    assert bot.user_states[chat_id]["state"] == bot.S.METER_SELECT


def confirm_state(scales=TWO_SCALES, values=("1.0", "2.0", "3.0", "4.0")) -> dict:
    return {
        "state": "confirm",
        "ls": "LS",
        "meters": [meter(scales, meter_type="Двухтарифный")],
        "meter_idx": 0,
        "meter_values": list(values),
    }


def test_confirm_persists_header_and_ordered_values_and_reopens_meter_list() -> None:
    deps = make_deps()
    state = confirm_state()
    readings.confirm(7, state, deps)
    deps.add_reading.assert_called_once_with(
        7,
        "LS",
        "Электроэнергия",
        "E-1",
        "METER-GUID",
        [
            {"scale_one_c_id": "S0", "scale_name": "Актив", "zone_one_c_id": "Z00",
             "zone_name": "День", "value": "1.0"},
            {"scale_one_c_id": "S0", "scale_name": "Актив", "zone_one_c_id": "Z01",
             "zone_name": "Ночь", "value": "2.0"},
            {"scale_one_c_id": "S1", "scale_name": "Реактив", "zone_one_c_id": "Z10",
             "zone_name": "День", "value": "3.0"},
            {"scale_one_c_id": "S1", "scale_name": "Реактив", "zone_one_c_id": "Z11",
             "zone_name": "Ночь", "value": "4.0"},
        ],
    )
    deps.clear_flow.assert_called_once_with(state)
    deps.show_meter_select.assert_called_once_with(7, "LS")
    assert deps.send_message.call_args.args[1] == (
        "✅ Показания по счётчику Электроэнергия №E-1 приняты!"
    )


def test_confirm_in_integration_mode_says_queued() -> None:
    deps = make_deps(integration_enabled=True)
    readings.confirm(7, confirm_state(), deps)
    assert deps.send_message.call_args.args[1] == (
        "✅ Показания по счётчику Электроэнергия №E-1 "
        "сохранены и ожидают обработки в 1С."
    )


def test_confirm_with_incomplete_values_writes_nothing_and_restarts() -> None:
    deps = make_deps()
    state = confirm_state(values=("1.0", "2.0"))
    readings.confirm(7, state, deps)
    deps.add_reading.assert_not_called()
    deps.reset_meter_input.assert_called_once_with(state)
    deps.ask_meter_value.assert_called_once_with(7)


def test_confirm_database_error_preserves_state_and_allows_retry() -> None:
    add_reading = MagicMock(side_effect=[RuntimeError("database unavailable"), None])
    deps = make_deps(add_reading=add_reading)
    state = confirm_state()
    before = dict(state, meter_values=list(state["meter_values"]))
    readings.confirm(7, state, deps)
    assert state == before
    deps.clear_flow.assert_not_called()
    deps.show_meter_select.assert_not_called()
    assert "Попробуйте подтвердить" in deps.send_message.call_args.args[1]

    deps.send_message.reset_mock()
    readings.confirm(7, state, deps)
    assert add_reading.call_count == 2
    deps.clear_flow.assert_called_once_with(state)
    deps.show_meter_select.assert_called_once_with(7, "LS")
    assert "приняты!" in deps.send_message.call_args.args[1]


def test_confirm_database_error_does_not_log_reading_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_ls = "SECRET-LS-908172"
    sentinel_value1 = "918273.4"
    sentinel_value2 = "564738.2"
    exception_text = (
        f"storage rejected {sentinel_ls} values {sentinel_value1}/{sentinel_value2}"
    )
    logger = logging.getLogger("test.readings.private-error")
    deps = make_deps(
        add_reading=MagicMock(side_effect=RuntimeError(exception_text)),
        logger=logger,
    )
    state = confirm_state(TWO_ZONES, (sentinel_value1, sentinel_value2))
    state["ls"] = sentinel_ls
    before = dict(state, meter_values=list(state["meter_values"]))

    with caplog.at_level(logging.ERROR, logger=logger.name):
        readings.confirm(7, state, deps)

    output = caplog.text
    assert "Не удалось сохранить показания счётчика" in output
    assert sentinel_ls not in output
    assert sentinel_value1 not in output
    assert sentinel_value2 not in output
    assert exception_text not in output
    assert all(record.exc_info is None for record in caplog.records)
    assert state == before
    deps.clear_flow.assert_not_called()
    deps.show_meter_select.assert_not_called()
    assert "Попробуйте подтвердить" in deps.send_message.call_args.args[1]


def test_bot_wrappers_resolve_runtime_patch_points() -> None:
    state = {"ls": "LS", "meters": [meter()], "meter_idx": 0}
    with (
        patch.object(bot, "_parse_reading", return_value="15") as parse,
        patch.object(bot, "_confirm_meter_reading") as confirm,
    ):
        bot._on_value(7, state, "15")
    parse.assert_called_once_with(7, "15")
    confirm.assert_called_once_with(7)
    assert state["meter_values"] == ["15"]


def test_bot_start_wrapper_resolves_saved_ls_and_request_hooks() -> None:
    with (
        patch.object(bot, "_get_saved_ls", return_value=None),
        patch.object(bot, "_request_ls") as request,
    ):
        bot._start_pokazaniya(7)
    request.assert_called_once_with(7, "pokazaniya")


def test_1c_auth_continues_deferred_readings_flow() -> None:
    chat_id = 778
    state = bot._get_state(chat_id)
    state.update(
        {
            "state": bot.S.AWAIT_LS_1C,
            "after_1c_auth": "pokazaniya",
        }
    )
    with (
        patch.object(bot, "_validate_ls", return_value=True),
        patch.object(bot, "_save_ls"),
        patch.object(bot, "_is_authenticated", return_value=True),
        patch.object(bot, "_show_meter_select") as show_meter_select,
    ):
        bot._on_await_ls_1c(chat_id, state, "100001")
    show_meter_select.assert_called_once_with(chat_id, "100001")
    assert state["state"] == bot.S.MENU
    assert "after_1c_auth" not in state


# Импорт 1С принимает только GUID; шкалы и зоны идут не в алфавитном порядке GUID.
ACC_1 = "10000000-0000-4000-8000-000000000001"
POINT_1 = "20000000-0000-4000-8000-000000000001"
METER_1 = "30000000-0000-4000-8000-000000000001"
SC_B = "4b000000-0000-4000-8000-000000000001"
SC_A = "4a000000-0000-4000-8000-000000000001"
TZ_B = "5b000000-0000-4000-8000-000000000001"
TZ_A = "5a000000-0000-4000-8000-000000000001"


def test_bot_flow_on_imported_meters_writes_values_and_hides_legacy_meters(
    tmp_path, monkeypatch
) -> None:
    """Real DB: 1C meters only, lower value accepted, header+values persisted."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "readings.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    db.import_from_1c_json(json.dumps([{
        "PersonalAccountID": ACC_1,
        "RoomKeyID": "11111111-1111-1111-1111-111111111111",
        "PersonalAccount": "LS-1",
        "MeteringPoints": [{
            "MeteringPointID": POINT_1,
            "MeteringPoint": "Ввод",
            "ResourceID": "RES-1",
            "Resource": "Электроэнергия",
            "Meters": [{
                "MeterID": METER_1,
                "MeterNumber": "123",
                "DifferentiationTypeID": "TYPE-2",
                "DifferentiationType": "Двухтарифный",
                "Scales": [
                    {"ScaleID": SC_B, "Scale": "Актив", "TariffZones": [
                        {"TariffZoneID": TZ_B, "TariffZone": "День"},
                        {"TariffZoneID": TZ_A, "TariffZone": "Ночь"},
                    ]},
                    {"ScaleID": SC_A, "Scale": "Реактив", "TariffZones": [
                        {"TariffZoneID": TZ_B, "TariffZone": "День"},
                        {"TariffZoneID": TZ_A, "TariffZone": "Ночь"},
                    ]},
                ],
            }],
        }],
    }]))
    db.upsert_bot_user(42, "LS-1", "User", authorized_1c=True)
    db.upsert_1c_meters(
        "LS-1",
        [{"meter_number": "LEGACY-1", "resource_type": "Вода", "meter_type": "Однотарифный"}],
    )
    # Earlier legacy reading for the same meter number: lower input is still accepted.
    db.add_pokazaniya(42, "LS-1", "Электроэнергия", "123", "1000")
    bot.user_states.clear()
    send_buttons = Mock(return_value=True)
    send_message = Mock(return_value=True)
    monkeypatch.setattr(bot, "ENABLE_1C_INTEGRATION", False)
    monkeypatch.setattr(bot, "send_buttons", send_buttons)
    monkeypatch.setattr(bot, "send_message", send_message)

    bot._start_pokazaniya(42)
    labels = [row[0]["text"] for row in send_buttons.call_args.args[2]]
    assert labels == ["Электроэнергия №123 (Двухтарифный)", "🏠 Главное меню"]

    state = bot._get_state(42)
    bot._cb_select_meter(42, state, "0")
    for text in ("5", "6,25", "0007", "12345678901.500000"):
        bot._on_value(42, state, text)
    assert [call.args[1] for call in send_message.call_args_list] == [
        "Электроэнергия №123 (Двухтарифный)\nАктив, День:",
        "Актив, Ночь:",
        "Реактив, День:",
        "Реактив, Ночь:",
    ]
    assert send_buttons.call_args.args[1] == (
        "Проверьте показания:\nЭлектроэнергия №123 (Двухтарифный)\n"
        "Актив, День: 5\nАктив, Ночь: 6.25\nРеактив, День: 7\nРеактив, Ночь: 12345678901.5"
    )
    bot._cb_meter_confirm(42, state)

    assert "приняты!" in send_message.call_args_list[-1].args[1]
    assert state["state"] == bot.S.METER_SELECT
    connection = db.get_conn()
    try:
        header = connection.execute(
            "SELECT * FROM pokazaniya ORDER BY id DESC LIMIT 1"
        ).fetchone()
        values = [
            tuple(row) for row in connection.execute(
                "SELECT scale_one_c_id, scale_name, zone_one_c_id, zone_name, value "
                "FROM pokazaniya_values WHERE pokazaniya_id=? ORDER BY id",
                (header["id"],),
            )
        ]
    finally:
        connection.close()
    assert (header["ls"], header["meter_number"], header["meter_one_c_id"]) == (
        "LS-1", "123", METER_1
    )
    assert (header["value1"], header["value2"]) == (None, None)
    assert values == [
        (SC_B, "Актив", TZ_B, "День", "5"),
        (SC_B, "Актив", TZ_A, "Ночь", "6.25"),
        (SC_A, "Реактив", TZ_B, "День", "7"),
        (SC_A, "Реактив", TZ_A, "Ночь", "12345678901.5"),
    ]
    bot.user_states.clear()
