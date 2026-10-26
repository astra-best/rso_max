from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path

import pytest

import database as db
from scripts.generate_synthetic_1c_json import DUPLICATE_METER_ID, build_payload

EXAMPLE_EXPORT = Path(__file__).resolve().parent.parent / "docs" / "JsonПример.txt"
EMPTY_1C_GUID = "00000000-0000-0000-0000-000000000000"


def guid(name: str) -> str:
    """Детерминированный GUID для читаемого имени: ID в выгрузке 1С — только GUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rso-test:{name}"))


def import_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "rso.database" and record.levelno == logging.WARNING
    ]


def table_count(table: str) -> int:
    connection = db.get_conn()
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
    finally:
        connection.close()


@pytest.fixture
def json_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "json-import.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    return db


def meter(
    meter_id: str = guid("METER-1"),
    *,
    meter_number: str = "SN-001",
    tariff_zones: int = 2,
) -> dict:
    return {
        "MeterID": meter_id,
        "MeterNumber": meter_number,
        "DifferentiationTypeID": "TYPE-2T",
        "DifferentiationType": {1: "Однотарифный", 2: "Двухтарифный"}.get(
            tariff_zones, "Трёхтарифный"
        ),
        "Scales": [
            {
                "ScaleID": guid("SCALE-1"),
                "Scale": "Основная",
                "TariffZones": [
                    {"TariffZoneID": guid(f"ZONE-{index}"), "TariffZone": f"Тариф {index}"}
                    for index in range(1, tariff_zones + 1)
                ],
            }
        ],
    }


def point(
    point_id: str = guid("POINT-1"),
    *,
    meters: list[dict] | None = None,
) -> dict:
    return {
        "MeteringPointID": point_id,
        "MeteringPoint": "Точка учёта",
        "ResourceID": "RESOURCE-1",
        "Resource": "Электроэнергия",
        "Meters": meters or [],
    }


def account(
    number: str,
    account_id: str,
    *,
    points: list[dict] | None = None,
    **fields,
) -> dict:
    return {
        "PersonalAccountID": account_id,
        "RoomKeyID": "11111111-1111-1111-1111-111111111111",
        "PersonalAccount": number,
        "MeteringPoints": points or [],
        **fields,
    }


def test_import_stores_nested_entities_and_skips_point_repeated_under_other_account(
    json_db, caplog
):
    db.create_lschet("100-1", "Existing Owner", "Existing address")
    connection = db.get_conn()
    connection.execute(
        "INSERT INTO schetchiki (ls, resource_type, meter_number, meter_type, initial1, initial2) "
        "VALUES ('100-1', 'Вода', 'LEGACY-1', 'Однотарифный', '10', '0')"
    )
    connection.commit()
    connection.close()
    db.add_pokazaniya(11, "100-1", "Вода", "LEGACY-1", "12")
    readings_before = len(db.get_pokazaniya())

    summary = db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1"), points=[point(meters=[meter()])]),
                account("100-2", guid("ACCOUNT-2"), points=[point(meters=[])]),
                account(
                    "100-3",
                    guid("ACCOUNT-3"),
                    points=[
                        point(
                            guid("POINT-3"),
                            meters=[
                                meter(
                                    guid("METER-3"),
                                    meter_number="SN-003",
                                    tariff_zones=1,
                                )
                            ],
                        )
                    ],
                ),
                account(
                    "100-4",
                    guid("ACCOUNT-4"),
                    points=[
                        point(
                            guid("POINT-4"),
                            meters=[
                                meter(
                                    guid("METER-4"),
                                    meter_number="SN-004",
                                    tariff_zones=3,
                                )
                            ],
                        )
                    ],
                ),
            ]
        )
    )

    existing_account = db.get_ls("100-1")
    assert existing_account["fio"] == "Existing Owner"
    assert existing_account["address"] == "Existing address"
    assert existing_account["one_c_id"] == guid("ACCOUNT-1")
    assert isinstance(existing_account["id"], int)
    assert [row["meter_number"] for row in db.get_schetchiki("100-1")] == [
        "SN-001",
        "LEGACY-1",
    ]
    # POINT-1 already belongs to 100-1, so its repetition under 100-2 is dropped.
    assert db.get_ls("100-2")["one_c_id"] == guid("ACCOUNT-2")
    assert db.get_schetchiki("100-2") == []
    assert db.get_schetchiki("100-1")[0]["meter_type"] == "Двухтарифный"
    assert db.get_schetchiki("100-3")[0]["meter_type"] == "Однотарифный"
    # Тип — название дифференциации из 1С, а не вывод из числа зон.
    assert db.get_schetchiki("100-4")[0]["meter_type"] == "Трёхтарифный"
    assert len(db.get_pokazaniya()) == readings_before
    assert summary == {
        "accounts_imported": 4,
        "points_imported": 3,
        "meters_imported": 3,
        "scales_imported": 3,
        "zones_imported": 6,
        "skipped": 1,
    }
    warnings = import_warnings(caplog)
    assert any(
        "[1].MeteringPoints[0]" in message
        and f"MeteringPointID={guid('POINT-1')!r}" in message
        and "100-2" in message
        for message in warnings
    )

    connection = db.get_conn()
    try:
        point_row = connection.execute(
            "SELECT id, one_c_id, licschet_id FROM metering_points WHERE one_c_id=?",
            (guid("POINT-1"),),
        ).fetchone()
        assert point_row["licschet_id"] == existing_account["id"]
        meter_row = connection.execute(
            "SELECT id, one_c_id, metering_point_id FROM metering_meters "
            "WHERE one_c_id=?",
            (guid("METER-1"),),
        ).fetchone()
        assert isinstance(point_row["id"], int)
        assert point_row["id"] != point_row["one_c_id"]
        assert isinstance(meter_row["id"], int)
        assert meter_row["id"] != meter_row["one_c_id"]
        assert meter_row["metering_point_id"] == point_row["id"]
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_points"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_meter_scales WHERE meter_id=?",
            (meter_row["id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_tariff_zones"
        ).fetchone()[0] == 6
    finally:
        connection.close()


def test_import_skips_blank_account_and_all_accounts_with_duplicate_number(json_db, caplog):
    shared_meter = meter(guid("METER-ON-SHARED-POINT"))
    summary = db.import_from_1c_json(
        json.dumps(
            [
                account("   ", guid("EMPTY-ACCOUNT"), points=[point(meters=[shared_meter])]),
                account("100-1", guid("ACCOUNT-1"), points=[point(meters=[shared_meter])]),
                account(
                    " 100-1 ",
                    guid("DUPLICATE-ACCOUNT"),
                    points=[point(guid("IGNORED-POINT"), meters=[shared_meter])],
                ),
                account("100-2", guid("ACCOUNT-2"), points=[point(meters=[shared_meter])]),
            ]
        )
    )

    connection = db.get_conn()
    try:
        assert [row[0] for row in connection.execute(
            "SELECT number FROM licschet ORDER BY number"
        )] == ["100-2"]
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_points WHERE one_c_id=?",
            (guid("IGNORED-POINT"),),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    # Skipped duplicates must not reserve POINT-1/METER for the valid account.
    assert [row["meter_number"] for row in db.get_schetchiki("100-2")] == ["SN-001"]
    assert summary["accounts_imported"] == 1
    assert summary["points_imported"] == 1
    assert summary["meters_imported"] == 1
    assert summary["skipped"] == 3
    warnings = import_warnings(caplog)
    assert any("[0]" in message and guid("EMPTY-ACCOUNT") in message for message in warnings)
    assert any(
        "[1]:" in message and "100-1" in message and guid("ACCOUNT-1") in message
        for message in warnings
    )
    assert any(
        "[2]:" in message and "100-1" in message and guid("DUPLICATE-ACCOUNT") in message
        for message in warnings
    )


def test_invalid_account_number_is_skipped_and_logged(json_db, caplog):
    summary = db.import_from_1c_json(
        json.dumps(
            [
                account("100 1/бад", guid("BAD-NUMBER"), points=[point(meters=[meter()])]),
                account("100-2", guid("ACCOUNT-2")),
            ]
        )
    )

    assert table_count("licschet") == 1
    assert db.get_ls("100-2") is not None
    assert table_count("metering_meters") == 0
    assert summary["accounts_imported"] == 1
    assert summary["skipped"] == 1
    assert any(
        "[0]" in message and guid("BAD-NUMBER") in message
        for message in import_warnings(caplog)
    )


def test_repeated_meter_id_skips_later_meter_and_imports_the_rest(json_db, caplog):
    db.create_lschet("100-1", "Original", "Original address")
    payload = [
        account("100-1", guid("ACCOUNT-1-UPDATED")),
        account("100-2", guid("ACCOUNT-2"), points=[point(meters=[meter(guid("DUPLICATE"))])]),
        account(
            "100-3",
            guid("ACCOUNT-3"),
            points=[
                point(
                    guid("POINT-3"),
                    meters=[
                        meter(guid("DUPLICATE"), meter_number="SN-DUP"),
                        meter(guid("METER-3"), meter_number="SN-003"),
                    ],
                )
            ],
        ),
    ]

    summary = db.import_from_1c_json(json.dumps(payload))

    assert db.get_ls("100-1")["one_c_id"] == guid("ACCOUNT-1-UPDATED")
    assert db.get_ls("100-1")["fio"] == "Original"
    assert [row["meter_number"] for row in db.get_schetchiki("100-2")] == ["SN-001"]
    assert [row["meter_number"] for row in db.get_schetchiki("100-3")] == ["SN-003"]
    assert summary["meters_imported"] == 2
    assert summary["skipped"] == 1
    assert any(
        "[2].MeteringPoints[0].Meters[0]" in message
        and f"MeterID={guid('DUPLICATE')!r}" in message
        and "100-3" in message
        for message in import_warnings(caplog)
    )


def test_repeated_personal_account_id_skips_later_account(json_db, caplog):
    db.create_lschet("100-1", "Original", "Original address")
    payload = [
        account("100-1", guid("SAME-ACCOUNT-ID")),
        account("100-2", guid("SAME-ACCOUNT-ID"), points=[point(meters=[meter()])]),
        account("100-3", guid("ACCOUNT-3")),
    ]

    summary = db.import_from_1c_json(json.dumps(payload))

    assert db.get_ls("100-1")["fio"] == "Original"
    assert db.get_ls("100-1")["one_c_id"] == guid("SAME-ACCOUNT-ID")
    assert db.get_ls("100-2") is None
    assert db.get_ls("100-3") is not None
    assert table_count("metering_points") == 0
    assert summary["accounts_imported"] == 2
    assert summary["skipped"] == 1
    assert any(
        "[1]: повтор PersonalAccountID в выгрузке" in message
        and guid("SAME-ACCOUNT-ID") in message and "100-2" in message
        for message in import_warnings(caplog)
    )


def test_personal_account_id_bound_to_other_number_in_db_is_skipped(json_db, caplog):
    db.import_from_1c_json(json.dumps([account("100-1", guid("ACCOUNT-1"))]))
    caplog.clear()

    summary = db.import_from_1c_json(
        json.dumps(
            [
                account("100-9", guid("ACCOUNT-1"), points=[point(meters=[meter()])]),
                account("100-2", guid("ACCOUNT-2"), points=[point(guid("POINT-2"))]),
            ]
        )
    )

    assert db.get_ls("100-9") is None
    assert db.get_ls("100-1")["one_c_id"] == guid("ACCOUNT-1")
    assert db.get_ls("100-2")["one_c_id"] == guid("ACCOUNT-2")
    assert table_count("metering_meters") == 0
    assert summary["accounts_imported"] == 1
    assert summary["points_imported"] == 1
    assert summary["skipped"] == 1
    assert any(
        "[0]" in message and guid("ACCOUNT-1") in message and "100-9" in message
        for message in import_warnings(caplog)
    )


def test_missing_guids_skip_only_the_affected_element(json_db, caplog):
    no_zone_id = meter(guid("METER-Z"), meter_number="SN-Z")
    no_zone_id["Scales"][0]["TariffZones"][1]["TariffZoneID"] = " "
    no_scale_id = meter(guid("METER-S"), meter_number="SN-S")
    no_scale_id["Scales"].append({"ScaleID": None, "Scale": "Bad", "TariffZones": []})
    payload = [
        account("100-0", None, points=[point(guid("POINT-0"), meters=[meter(guid("METER-0"))])]),
        account(
            "100-1",
            guid("ACCOUNT-1"),
            points=[
                {"MeteringPoint": "No id", "Meters": [meter(guid("METER-NO-POINT"))]},
                point(
                    guid("POINT-1"),
                    meters=[
                        {"MeterNumber": "SN-NO-ID", "Scales": []},
                        meter(guid("METER-BLANK"), meter_number=" "),
                        no_zone_id,
                        no_scale_id,
                        "not-an-object",
                    ],
                ),
            ],
        ),
    ]

    summary = db.import_from_1c_json(json.dumps(payload))

    assert db.get_ls("100-0") is None
    assert sorted(row["meter_number"] for row in db.get_schetchiki("100-1")) == [
        "SN-S",
        "SN-Z",
    ]
    assert table_count("metering_points") == 1
    assert summary == {
        "accounts_imported": 1,
        "points_imported": 1,
        "meters_imported": 2,
        "scales_imported": 2,
        "zones_imported": 3,
        "skipped": 7,
    }
    warnings = "\n".join(import_warnings(caplog))
    assert "[0]" in warnings and "PersonalAccountID" in warnings
    assert "[1].MeteringPoints[0]" in warnings
    assert "[1].MeteringPoints[1].Meters[0]" in warnings
    assert "[1].MeteringPoints[1].Meters[1]" in warnings and guid("METER-BLANK") in warnings
    assert "[1].MeteringPoints[1].Meters[2].Scales[0].TariffZones[1]" in warnings
    assert "[1].MeteringPoints[1].Meters[3].Scales[1]" in warnings
    assert "[1].MeteringPoints[1].Meters[4]" in warnings


def test_non_array_nested_collections_are_skipped(json_db, caplog):
    bad_scales = meter(guid("METER-2"), meter_number="SN-2")
    bad_scales["Scales"] = {"ScaleID": "S"}
    bad_zones = meter(guid("METER-3"), meter_number="SN-3")
    bad_zones["Scales"][0]["TariffZones"] = "zones"
    summary = db.import_from_1c_json(
        json.dumps(
            [
                {
                    "PersonalAccountID": guid("A-1"),
                    "PersonalAccount": "100-1",
                    "RoomKeyID": "11111111-1111-1111-1111-111111111111",
                    "MeteringPoints": {},
                },
                account(
                    "100-2",
                    guid("A-2"),
                    points=[
                        {**point(guid("P-2")), "Meters": "x"},
                        point(guid("P-3"), meters=[bad_scales, bad_zones]),
                    ],
                ),
                "not-an-object",
            ]
        )
    )

    assert db.get_ls("100-1") is not None
    assert summary == {
        "accounts_imported": 2,
        "points_imported": 2,
        "meters_imported": 2,
        "scales_imported": 1,
        "zones_imported": 0,
        "skipped": 5,
    }
    warnings = "\n".join(import_warnings(caplog))
    assert "[0].MeteringPoints" in warnings
    assert "[1].MeteringPoints[0].Meters" in warnings
    assert "[1].MeteringPoints[1].Meters[0].Scales" in warnings
    assert "[1].MeteringPoints[1].Meters[1].Scales[0].TariffZones" in warnings
    assert "[2]" in warnings


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe not utf-8",
        "[{\"PersonalAccount\": \"100-1\",".encode(),
        json.dumps({"PersonalAccount": "100-1", "PersonalAccountID": guid("A-1")}),
    ],
)
def test_unreadable_file_is_rejected_entirely(json_db, payload):
    with pytest.raises(ValueError):
        db.import_from_1c_json(payload)

    assert table_count("licschet") == 0
    assert table_count("metering_points") == 0


def test_point_moved_to_other_account_on_reupload_updates_owner(json_db):
    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1"), points=[point(guid("POINT-1"), meters=[meter()])]),
                account("100-2", guid("ACCOUNT-2")),
            ]
        )
    )
    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1")),
                account("100-2", guid("ACCOUNT-2"), points=[point(guid("POINT-1"), meters=[meter()])]),
            ]
        )
    )

    assert db.get_schetchiki("100-1") == []
    assert [row["meter_number"] for row in db.get_schetchiki("100-2")] == ["SN-001"]
    assert table_count("metering_points") == 1


def test_reimport_of_same_file_is_idempotent(json_db):
    payload = json.dumps(
        [
            account("100-1", guid("ACCOUNT-1"), points=[point(meters=[meter()])]),
            account("100-1", guid("DUPLICATE")),
            account("100-2", guid("ACCOUNT-2"), points=[point(guid("POINT-2"), meters=[meter(guid("M-2"))])]),
        ]
    )
    first = db.import_from_1c_json(payload)
    tables = (
        "licschet", "metering_points", "metering_meters",
        "metering_meter_scales", "metering_tariff_zones",
    )
    counts_after_first = {table: table_count(table) for table in tables}

    second = db.import_from_1c_json(payload)

    assert second == first
    assert {table: table_count(table) for table in tables} == counts_after_first
    assert counts_after_first["licschet"] == 1


def test_unexpected_error_rolls_back_the_whole_import(json_db):
    db.create_lschet("100-1", "Original", "Original address")
    connection = db.get_conn()
    connection.execute(
        "CREATE TRIGGER fail_meter_insert BEFORE INSERT ON metering_meters "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    connection.commit()
    connection.close()

    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        db.import_from_1c_json(
            json.dumps(
                [
                    account("100-1", guid("ACCOUNT-1")),
                    account("100-2", guid("ACCOUNT-2"), points=[point(meters=[meter()])]),
                ]
            )
        )

    assert db.get_ls("100-1")["fio"] == "Original"
    assert db.get_ls("100-1")["one_c_id"] is None
    assert db.get_ls("100-2") is None
    assert table_count("metering_points") == 0


def test_fio_and_address_in_json_are_ignored(json_db):
    db.create_lschet("100-1", "Existing Owner", "Existing address")

    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1"), FIO="Other", Address="Other address"),
                account("100-2", guid("ACCOUNT-2"), FIO="New owner", Address="New address"),
                account("100-3", guid("ACCOUNT-3"), FIO={"bad": "type"}, Address="\n"),
            ]
        )
    )

    assert db.get_ls("100-1")["fio"] == "Existing Owner"
    assert db.get_ls("100-1")["address"] == "Existing address"
    assert db.get_ls("100-1")["one_c_id"] == guid("ACCOUNT-1")
    assert db.get_ls("100-2")["fio"] is None
    assert db.get_ls("100-2")["address"] is None
    assert db.get_ls("100-3") is not None


def test_wrong_typed_text_fields_skip_only_their_element(json_db, caplog):
    bad_point = point(guid("POINT-BAD"), meters=[meter(guid("METER-IN-BAD-POINT"))])
    bad_point["Resource"] = {"name": "Вода"}
    bad_meter = meter(guid("METER-BAD"), meter_number="SN-BAD")
    bad_meter["DifferentiationType"] = ["Двухтарифный"]
    bad_scale = meter(guid("METER-SCALE"), meter_number="SN-SCALE")
    bad_scale["Scales"].append(
        {"ScaleID": guid("SCALE-2"), "Scale": 7, "TariffZones": [{"TariffZoneID": guid("Z"), "TariffZone": "T"}]}
    )
    bad_zone = meter(guid("METER-ZONE"), meter_number="SN-ZONE")
    bad_zone["Scales"][0]["TariffZones"][0]["TariffZone"] = {"x": 1}
    bad_number = meter(guid("METER-NUMBER"), meter_number="unused")
    bad_number["MeterNumber"] = {"serial": "1"}
    int_number = meter(guid("METER-INT"), meter_number="unused")
    int_number["MeterNumber"] = 12345

    summary = db.import_from_1c_json(
        json.dumps(
            [
                account(
                    "100-1",
                    guid("ACCOUNT-1"),
                    points=[
                        bad_point,
                        point(
                            guid("POINT-1"),
                            meters=[bad_meter, bad_scale, bad_zone, bad_number, int_number],
                        ),
                    ],
                )
            ]
        )
    )

    assert summary == {
        "accounts_imported": 1,
        "points_imported": 1,
        "meters_imported": 2,
        "scales_imported": 2,
        "zones_imported": 3,
        "skipped": 6,
    }
    assert sorted(row["meter_number"] for row in db.get_schetchiki("100-1")) == [
        "SN-SCALE",
        "SN-ZONE",
    ]
    warnings = "\n".join(import_warnings(caplog))
    assert "[0].MeteringPoints[0]: некорректный тип поля Resource" in warnings
    assert "Meters[0]: некорректный тип поля DifferentiationType" in warnings
    assert "Meters[1].Scales[1]: некорректный тип поля Scale" in warnings
    assert "Meters[2].Scales[0].TariffZones[0]: некорректный тип поля TariffZone" in warnings
    assert "Meters[3]: некорректный тип поля MeterNumber" in warnings
    assert "Meters[4]: некорректный тип поля MeterNumber" in warnings


def test_non_string_account_number_is_skipped_without_logging_content(json_db, caplog):
    summary = db.import_from_1c_json(
        json.dumps(
            [
                account(100123, guid("ACCOUNT-1")),
                account(True, guid("ACCOUNT-2")),
                account({"Номер": "SECRET-PD"}, guid("ACCOUNT-3")),
                account("100-4", guid("ACCOUNT-4")),
            ]
        )
    )

    assert summary["accounts_imported"] == 1
    assert summary["skipped"] == 3
    assert db.get_ls("100-4") is not None
    assert db.get_ls("100123") is None
    assert db.get_ls("True") is None
    warnings = import_warnings(caplog)
    assert sum("некорректный тип поля PersonalAccount" in m for m in warnings) == 3
    assert not any("SECRET-PD" in m for m in warnings)


def test_non_string_account_number_does_not_count_as_duplicate(json_db):
    summary = db.import_from_1c_json(
        json.dumps([account(100123, guid("ACCOUNT-1")), account("100123", guid("ACCOUNT-2"))])
    )

    assert summary["accounts_imported"] == 1
    assert db.get_ls("100123")["one_c_id"] == guid("ACCOUNT-2")


def test_room_key_is_stored_shared_and_updated_on_reimport(json_db):
    room_a = "30b58665-7da8-48cf-8da6-d78eeb2ecdaa"
    room_b = "4C1D2E3F-0000-4000-8000-0000000000AB"
    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1"), RoomKeyID=room_a),
                account("100-2", guid("ACCOUNT-2"), RoomKeyID=room_a),
            ]
        )
    )
    assert [db.get_ls(number)["room_key_id"] for number in ("100-1", "100-2")] == [
        room_a,
        room_a,
    ]

    db.import_from_1c_json(
        json.dumps([account("100-1", guid("ACCOUNT-1"), RoomKeyID=room_b)])
    )

    assert db.get_ls("100-1")["room_key_id"] == room_b


def test_room_key_defaults_to_empty_1c_reference_unless_guid(json_db, caplog):
    missing = account("100-1", guid("ACCOUNT-1"))
    del missing["RoomKeyID"]
    empty_guid = "00000000-0000-0000-0000-000000000000"
    summary = db.import_from_1c_json(
        json.dumps(
            [
                missing,
                account("100-2", guid("ACCOUNT-2"), RoomKeyID={"room": "SECRET"}),
                account("100-3", guid("ACCOUNT-3"), RoomKeyID="  "),
                account("100-4", guid("ACCOUNT-4"), RoomKeyID=None),
                account("100-5", guid("ACCOUNT-5"), RoomKeyID=12345),
                account("100-6", guid("ACCOUNT-6"), RoomKeyID="ROOM-SECRET"),
                account("100-7", guid("ACCOUNT-7"), RoomKeyID="30b58665-7da8-48cf-8da6-d78eeb2ecda"),
                account("100-8", guid("ACCOUNT-8"), RoomKeyID=empty_guid),
            ]
        )
    )

    assert summary["accounts_imported"] == 8
    assert summary["skipped"] == 0
    assert [db.get_ls(f"100-{n}")["room_key_id"] for n in range(1, 9)] == [empty_guid] * 8
    warnings = import_warnings(caplog)
    # Поле есть, но не GUID: пустая строка, объект, число, не-GUID строки.
    assert sum("RoomKeyID не в формате GUID" in m for m in warnings) == 5
    assert not any("SECRET" in m for m in warnings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30b58665-7da8-48cf-8da6-d78eeb2ecdaa", True),
        ("4C1D2E3F-0000-4000-8000-0000000000AB", True),
        ("00000000-0000-0000-0000-000000000000", True),
        (" 30b58665-7da8-48cf-8da6-d78eeb2ecdaa", False),
        ("30b58665-7da8-48cf-8da6-d78eeb2ecda", False),
        ("30b58665-7da8-48cf-8da6-d78eeb2ecdaz", False),
        ("30b586657da848cf8da6d78eeb2ecdaa", False),
        ("30b58665-7da8-48cf-8da6-d78eeb2ecdaa\n", False),
        ("", False),
        (None, False),
        (12345, False),
        ({"guid": "30b58665-7da8-48cf-8da6-d78eeb2ecdaa"}, False),
    ],
)
def test_is_guid(value, expected):
    assert db._is_guid(value) is expected


def test_malformed_guids_are_skipped_and_never_logged_raw(json_db, caplog):
    summary = db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", {"guid": guid("ACCOUNT-1")}),
                account("100-2", "ACCOUNT-2\nWARNING forged line"),
                account("100-3", "A" * 65),
                account(
                    "100-4",
                    guid("ACCOUNT-4"),
                    points=[
                        point(123),
                        point(guid("POINT-4"), meters=[meter("METER\r\n-FORGED")]),
                    ],
                ),
                account("100-5\nWARNING forged", guid("ACCOUNT-5")),
                # Раньше пробельные символы по краям обрезались; теперь это не GUID.
                account("100-6", guid("ACCOUNT-6") + "\nWARNING forged"),
            ]
        )
    )

    assert [
        db.get_ls(number) is not None for number in ("100-1", "100-2", "100-3", "100-6")
    ] == [False, False, False, False]
    assert db.get_ls("100-4")["one_c_id"] == guid("ACCOUNT-4")
    assert db.get_schetchiki("100-4") == []
    assert summary["accounts_imported"] == 1
    assert summary["points_imported"] == 1
    assert summary["meters_imported"] == 0
    assert summary["skipped"] == 7
    warnings = import_warnings(caplog)
    assert sum("некорректный тип/формат ID PersonalAccountID" in m for m in warnings) == 4
    assert not any("forged line" in message or "-FORGED" in message for message in warnings)
    assert not any(guid("ACCOUNT-6") in message for message in warnings)
    assert any("[3].MeteringPoints[0]: некорректный тип/формат ID MeteringPointID" in m
               for m in warnings)
    assert any("[3].MeteringPoints[1].Meters[0]: некорректный тип/формат ID MeterID" in m
               for m in warnings)
    assert all("\n" not in message and "\r" not in message for message in warnings)
    assert not any("{'guid'" in message for message in warnings)
    connection = db.get_conn()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM licschet WHERE one_c_id LIKE '%guid%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def skipped_elements(caplog) -> list[str]:
    """Только сообщения о пропуске отдельных элементов, без итоговой строки."""
    return [m for m in import_warnings(caplog) if "пропущен элемент" in m]


# Путь пропускаемого элемента и итог импорта, когда ID именно этого поля плохой.
# Все ID корректны: 2 ЛС, 2 точки, 3 прибора, 4 шкалы, 7 зон.
BAD_ID_CASES = {
    "PersonalAccountID": (
        "[0]",
        {"accounts_imported": 1, "points_imported": 2, "meters_imported": 3,
         "scales_imported": 4, "zones_imported": 7, "skipped": 1},
    ),
    "MeteringPointID": (
        "[1].MeteringPoints[0]",
        {"accounts_imported": 2, "points_imported": 1, "meters_imported": 2,
         "scales_imported": 3, "zones_imported": 5, "skipped": 1},
    ),
    "MeterID": (
        "[1].MeteringPoints[1].Meters[0]",
        {"accounts_imported": 2, "points_imported": 2, "meters_imported": 2,
         "scales_imported": 3, "zones_imported": 5, "skipped": 1},
    ),
    "ScaleID": (
        "[1].MeteringPoints[1].Meters[1].Scales[0]",
        {"accounts_imported": 2, "points_imported": 2, "meters_imported": 3,
         "scales_imported": 3, "zones_imported": 6, "skipped": 1},
    ),
    "TariffZoneID": (
        "[1].MeteringPoints[1].Meters[1].Scales[1].TariffZones[0]",
        {"accounts_imported": 2, "points_imported": 2, "meters_imported": 3,
         "scales_imported": 4, "zones_imported": 6, "skipped": 1},
    ),
}


def payload_with_id(label: str, value) -> list[dict]:
    """Выгрузка, где ``value`` стоит в поле ``label`` ровно одного элемента."""

    def pick(field: str, name: str):
        return value if field == label else guid(name)

    two_scales = meter(guid("METER-1"), meter_number="SN-M1")
    two_scales["Scales"] = [
        {
            "ScaleID": pick("ScaleID", "SCALE-0"),
            "Scale": "Актив",
            "TariffZones": [{"TariffZoneID": guid("ZONE-S0"), "TariffZone": "Сутки"}],
        },
        {
            "ScaleID": guid("SCALE-1"),
            "Scale": "Реактив",
            "TariffZones": [
                {"TariffZoneID": pick("TariffZoneID", "ZONE-0"), "TariffZone": "День"},
                {"TariffZoneID": guid("ZONE-OK"), "TariffZone": "Ночь"},
            ],
        },
    ]
    return [
        account("100-0", pick("PersonalAccountID", "ACCOUNT-0")),
        account(
            "100-1",
            guid("ACCOUNT-1"),
            points=[
                point(
                    pick("MeteringPointID", "POINT-0"),
                    meters=[meter(guid("METER-P0"), meter_number="SN-P0")],
                ),
                point(
                    guid("POINT-1"),
                    meters=[
                        meter(pick("MeterID", "METER-0"), meter_number="SN-M0"),
                        two_scales,
                    ],
                ),
            ],
        ),
    ]


def test_payload_with_only_valid_ids_imports_everything(json_db, caplog):
    summary = db.import_from_1c_json(json.dumps(payload_with_id("none", None)))

    assert summary == {
        "accounts_imported": 2, "points_imported": 2, "meters_imported": 3,
        "scales_imported": 4, "zones_imported": 7, "skipped": 0,
    }
    assert db.get_ls("100-0")["one_c_id"] == guid("ACCOUNT-0")
    assert skipped_elements(caplog) == []


def test_upper_case_guid_is_a_valid_id(json_db):
    upper = guid("ACCOUNT-UPPER").upper()

    summary = db.import_from_1c_json(json.dumps([account("100-1", upper)]))

    assert summary["accounts_imported"] == 1
    assert db.get_ls("100-1")["one_c_id"] == upper


@pytest.mark.parametrize("label", list(BAD_ID_CASES))
def test_empty_1c_reference_in_id_is_skipped_as_missing(json_db, caplog, label):
    path, expected = BAD_ID_CASES[label]

    summary = db.import_from_1c_json(json.dumps(payload_with_id(label, EMPTY_1C_GUID)))

    assert summary == expected
    warnings = skipped_elements(caplog)
    assert len(warnings) == 1
    assert f"пропущен элемент {path}: отсутствует {label}" in warnings[0]
    assert EMPTY_1C_GUID not in warnings[0]


RAW_ID = guid("RAW-ID-UNDER-TEST")


@pytest.mark.parametrize("label", list(BAD_ID_CASES))
@pytest.mark.parametrize(
    "value",
    [
        "ACCOUNT-1-SECRET",
        " " + RAW_ID,
        RAW_ID + " ",
        RAW_ID[:-1],
        RAW_ID + "0",
        RAW_ID + "\n",
        "{" + RAW_ID + "}",
        RAW_ID.replace("-", ""),
        12345,
        ["list"],
    ],
    ids=[
        "placeholder", "leading-space", "trailing-space", "missing-char", "extra-char",
        "newline", "braces", "no-dashes", "int", "list",
    ],
)
def test_non_guid_id_is_skipped_with_format_error_and_not_logged(
    json_db, caplog, label, value
):
    path, expected = BAD_ID_CASES[label]

    summary = db.import_from_1c_json(json.dumps(payload_with_id(label, value)))

    assert summary == expected
    warnings = skipped_elements(caplog)
    assert len(warnings) == 1
    assert f"пропущен элемент {path}: некорректный тип/формат ID {label}" in warnings[0]
    assert "SECRET" not in warnings[0]
    assert RAW_ID[:-1] not in warnings[0]
    assert RAW_ID.replace("-", "") not in warnings[0]
    assert "12345" not in warnings[0] and "list" not in warnings[0]


def test_deleting_account_cascades_to_imported_metering_data(json_db):
    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1"), points=[point(meters=[meter()])]),
                account("100-2", guid("ACCOUNT-2"), points=[point(guid("POINT-2"), meters=[meter(guid("M-2"))])]),
            ]
        )
    )

    connection = db.get_conn()
    try:
        connection.execute("DELETE FROM licschet WHERE number='100-1'")
        connection.commit()
    finally:
        connection.close()

    assert table_count("metering_points") == 1
    assert table_count("metering_meters") == 1
    assert table_count("metering_meter_scales") == 1
    assert table_count("metering_tariff_zones") == 2
    assert [row["meter_number"] for row in db.get_schetchiki("100-2")] == ["SN-001"]


def test_schema_binds_point_to_single_account_without_link_table(json_db):
    connection = db.get_conn()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='licschet_metering_points'"
        ).fetchone()[0] == 0
        columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(metering_points)")
        }
        assert columns["licschet_id"]["notnull"] == 1
        foreign_keys = [
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(metering_points)")
        ]
        assert ("licschet_id", "licschet", "id", "CASCADE") in foreign_keys
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO metering_points(one_c_id, licschet_id) VALUES('P', NULL)"
            )
    finally:
        connection.close()


@pytest.mark.skipif(not EXAMPLE_EXPORT.exists(), reason="docs/JsonПример.txt is absent")
def test_example_export_imports_with_skips(json_db):
    summary = db.import_from_1c_json(EXAMPLE_EXPORT.read_bytes())

    # Expected values were derived from the file by a separate analysis script:
    # 5 numbers repeated twice (10 accounts), 1 blank number, 1 point already
    # owned by an earlier account; duplicate MeterIDs sit under skipped parents.
    assert summary == {
        "accounts_imported": 46,
        "points_imported": 29,
        "meters_imported": 29,
        "scales_imported": 29,
        "zones_imported": 34,
        "skipped": 12,
    }
    assert table_count("licschet") == 46
    assert table_count("metering_points") == 29
    assert table_count("metering_meters") == 29
    assert table_count("metering_tariff_zones") == 34
    connection = db.get_conn()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM licschet WHERE room_key_id IS NULL"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_personal_account_id_has_database_level_uniqueness(json_db):
    connection = db.get_conn()
    try:
        connection.execute(
            "INSERT INTO licschet(number, one_c_id) VALUES('100-1', 'EXTERNAL-1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO licschet(number, one_c_id) VALUES('100-2', 'EXTERNAL-1')"
            )
    finally:
        connection.close()


def test_reimport_upserts_existing_json_entities_and_never_deletes_absent_rows(json_db):
    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", guid("ACCOUNT-1"), points=[point(meters=[meter(guid("METER-1"))])]),
                account("100-2", guid("ACCOUNT-2")),
            ]
        )
    )
    db.import_from_1c_json(json.dumps([account("100-1", guid("ACCOUNT-1"))]))

    assert db.get_ls("100-2") is not None
    assert [row["meter_number"] for row in db.get_schetchiki("100-1")] == ["SN-001"]


def test_init_db_migration_preserves_legacy_meters_and_readings(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE licschet (id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, "
            "fio TEXT, address TEXT)"
        )
        connection.execute(
            "CREATE TABLE schetchiki (id INTEGER PRIMARY KEY, ls TEXT NOT NULL, "
            "resource_type TEXT, meter_number TEXT, meter_type TEXT, initial1 TEXT, initial2 TEXT)"
        )
        connection.execute(
            "CREATE TABLE pokazaniya (id INTEGER PRIMARY KEY, chat_id INTEGER, ls TEXT, "
            "resource_type TEXT, meter_number TEXT, value1 TEXT, value2 TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO licschet VALUES (17, '100-1', 'Owner', 'Address')"
        )
        connection.execute(
            "INSERT INTO schetchiki VALUES (23, '100-1', 'Вода', 'OLD-METER', "
            "'Однотарифный', '10', '0')"
        )
        connection.execute(
            "INSERT INTO pokazaniya VALUES (31, 42, '100-1', 'Вода', 'OLD-METER', "
            "'15', NULL, '2026-01-01 10:00')"
        )

    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    db.init_db()

    assert db.get_ls("100-1")["id"] == 17
    assert db.get_schetchiki("100-1")[0]["meter_number"] == "OLD-METER"
    assert db.get_last_pokazaniya("100-1", "OLD-METER")["value1"] == "15"
    connection = db.get_conn()
    try:
        account_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(licschet)")
        }
        assert "one_c_id" in account_columns
        assert connection.execute("SELECT id FROM schetchiki WHERE id=23").fetchone()[0] == 23
        reading = connection.execute("SELECT * FROM pokazaniya WHERE id=31").fetchone()
        assert (reading["value1"], reading["value2"], reading["created_at"]) == (
            "15", None, "2026-01-01 10:00"
        )
        assert reading["meter_one_c_id"] is None
        value_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(pokazaniya_values)")
        ]
        assert value_columns == [
            "id", "pokazaniya_id", "scale_one_c_id", "scale_name",
            "zone_one_c_id", "zone_name", "value",
        ]
        assert connection.execute("SELECT COUNT(*) FROM pokazaniya_values").fetchone()[0] == 0
    finally:
        connection.close()
    legacy_row = db.get_pokazaniya_with_prev()[0]
    assert (legacy_row["id"], legacy_row["value1"], legacy_row["values_text"]) == (
        31, "15", None
    )


# GUID шкал и зон убывают по алфавиту, а импортируются в порядке выгрузки.
SCALE_Z = "f0000000-0000-4000-8000-000000000001"
SCALE_A = "a0000000-0000-4000-8000-000000000001"
ZONE_Z = "f0000000-0000-4000-8000-000000000002"
ZONE_A = "a0000000-0000-4000-8000-000000000002"


def two_scale_meter() -> dict:
    """Прибор с двумя шкалами по две зоны; GUID шкалы/зоны специально не по алфавиту."""
    return {
        "MeterID": guid("METER-2S"),
        "MeterNumber": "123",
        "DifferentiationTypeID": "TYPE-2T",
        "DifferentiationType": "Двухтарифный",
        "Scales": [
            {
                "ScaleID": SCALE_Z,
                "Scale": "Актив",
                "TariffZones": [
                    {"TariffZoneID": ZONE_Z, "TariffZone": "День"},
                    {"TariffZoneID": ZONE_A, "TariffZone": "Ночь"},
                ],
            },
            {
                "ScaleID": SCALE_A,
                "Scale": "Реактив",
                "TariffZones": [
                    {"TariffZoneID": ZONE_Z, "TariffZone": "День"},
                    {"TariffZoneID": ZONE_A, "TariffZone": "Ночь"},
                ],
            },
        ],
    }


def test_bot_meters_follow_import_order_and_skip_meters_without_zones(json_db):
    without_zones = meter(guid("METER-EMPTY"), meter_number="EMPTY")
    without_zones["Scales"] = [{"ScaleID": guid("SCALE-E"), "Scale": "Основная"}]
    nameless = meter(guid("METER-NULL"), meter_number="NULL-TYPE", tariff_zones=1)
    nameless["DifferentiationType"] = None
    db.import_from_1c_json(
        json.dumps(
            [
                account(
                    "100-1",
                    guid("ACCOUNT-1"),
                    points=[point(meters=[two_scale_meter(), without_zones, nameless])],
                )
            ]
        )
    )
    connection = db.get_conn()
    connection.execute(
        "INSERT INTO schetchiki (ls, resource_type, meter_number, meter_type) "
        "VALUES ('100-1', 'Вода', 'LEGACY-1', 'Однотарифный')"
    )
    connection.commit()
    connection.close()

    meters = db.get_1c_meters_with_zones("100-1")

    assert [m["meter_number"] for m in meters] == ["123", "NULL-TYPE"]
    first = meters[0]
    assert (first["meter_one_c_id"], first["resource_type"], first["meter_type"]) == (
        guid("METER-2S"), "Электроэнергия", "Двухтарифный"
    )
    assert [zone["label"] for zone in first["zones"]] == [
        "Актив, День", "Актив, Ночь", "Реактив, День", "Реактив, Ночь"
    ]
    assert [(z["scale_one_c_id"], z["zone_one_c_id"]) for z in first["zones"]] == [
        (SCALE_Z, ZONE_Z), (SCALE_Z, ZONE_A),
        (SCALE_A, ZONE_Z), (SCALE_A, ZONE_A),
    ]
    assert meters[1]["meter_type"] is None
    assert db.get_1c_meters_with_zones("100-404") == []


def _values(*pairs) -> list[dict]:
    return [
        {
            "scale_one_c_id": scale_id,
            "scale_name": "Актив",
            "zone_one_c_id": zone_id,
            "zone_name": zone_name,
            "value": value,
        }
        for scale_id, zone_id, zone_name, value in pairs
    ]


def test_add_pokazaniya_values_writes_header_and_ordered_values(json_db, monkeypatch):
    monkeypatch.setattr(db, "msk_now", lambda: "2026-10-26 12:00")

    reading_id = db.add_pokazaniya_values(
        7, "100-1", "Электроэнергия", "123", "METER-2S",
        _values(("S", "Z-NIGHT", "Ночь", "45.0"), ("S", "Z-DAY", "День", "123.0")),
    )

    connection = db.get_conn()
    try:
        header = dict(connection.execute(
            "SELECT chat_id, ls, resource_type, meter_number, value1, value2, "
            "created_at, meter_one_c_id, sent_to_1c, batch_id FROM pokazaniya WHERE id=?",
            (reading_id,),
        ).fetchone())
        values = [
            tuple(row) for row in connection.execute(
                "SELECT scale_one_c_id, scale_name, zone_one_c_id, zone_name, value "
                "FROM pokazaniya_values WHERE pokazaniya_id=? ORDER BY id",
                (reading_id,),
            )
        ]
    finally:
        connection.close()
    assert header == {
        "chat_id": 7, "ls": "100-1", "resource_type": "Электроэнергия",
        "meter_number": "123", "value1": None, "value2": None,
        "created_at": "2026-10-26 12:00", "meter_one_c_id": "METER-2S",
        "sent_to_1c": 0, "batch_id": None,
    }
    assert values == [
        ("S", "Актив", "Z-NIGHT", "Ночь", "45.0"),
        ("S", "Актив", "Z-DAY", "День", "123.0"),
    ]
    portal_row = db.get_pokazaniya_with_prev()[0]
    assert portal_row["values_text"] == "Актив, Ночь: 45.0; Актив, День: 123.0"
    assert portal_row["prev_value1"] is None and portal_row["diff1"] is None


@pytest.mark.parametrize(
    "values",
    [
        # Повтор пары нарушает UNIQUE на второй строке значений.
        _values(("S", guid("Z"), "День", "1.0"), ("S", guid("Z"), "День", "2.0")),
        # Ошибка формирования второй строки (нет значения).
        _values(("S", "Z1", "День", "1.0"), ("S", "Z2", "Ночь", None)),
        [],
    ],
)
def test_add_pokazaniya_values_is_all_or_nothing(json_db, values):
    with pytest.raises(Exception):  # noqa: B017 - любой сбой должен откатить запись
        db.add_pokazaniya_values(7, "100-1", "Электроэнергия", "123", "M", values)

    assert table_count("pokazaniya") == 0
    assert table_count("pokazaniya_values") == 0


def test_init_db_keeps_duplicate_legacy_external_ids_and_import_checks_conflicts(
    tmp_path, monkeypatch
):
    path = tmp_path / "legacy-duplicate-external-ids.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE licschet (id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, "
            "fio TEXT, address TEXT, one_c_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO licschet VALUES (?, ?, ?, ?, ?)",
            [
                (17, "100-1", "Owner 1", "Address 1", guid("LEGACY-DUPLICATE")),
                (18, "100-2", "Owner 2", "Address 2", guid("LEGACY-DUPLICATE")),
            ],
        )

    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()

    connection = db.get_conn()
    try:
        assert [
            (row["id"], row["number"])
            for row in connection.execute("SELECT id, number FROM licschet ORDER BY id")
        ] == [(17, "100-1"), (18, "100-2")]
        indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(licschet)")
        }
        assert "uq_licschet_one_c_id" not in indexes
    finally:
        connection.close()

    summary = db.import_from_1c_json(
        json.dumps([
            account("100-3", guid("LEGACY-DUPLICATE")),
            account("100-4", guid("ACCOUNT-4")),
        ])
    )
    assert summary["accounts_imported"] == 1
    assert summary["skipped"] == 1
    assert db.get_ls("100-1")["fio"] == "Owner 1"
    assert db.get_ls("100-2")["fio"] == "Owner 2"
    assert db.get_ls("100-3") is None
    assert db.get_ls("100-4")["one_c_id"] == guid("ACCOUNT-4")


def test_synthetic_import_stage_is_limited_to_one_hundred_accounts(json_db):
    payload = build_payload(100)

    summary = db.import_from_1c_json(json.dumps(payload))

    assert summary["accounts_imported"] == 100
    connection = db.get_conn()
    try:
        assert connection.execute("SELECT COUNT(*) FROM licschet").fetchone()[0] == 100
        assert connection.execute("SELECT COUNT(*) FROM metering_meters").fetchone()[0] == 100
        assert connection.execute("SELECT COUNT(*) FROM pokazaniya").fetchone()[0] == 0
    finally:
        connection.close()
    with pytest.raises(ValueError, match="1000"):
        build_payload(1001)


def test_synthetic_duplicate_mode_skips_repeated_meter(json_db, caplog):
    payload = build_payload(2, duplicate_meter_id=True)

    summary = db.import_from_1c_json(json.dumps(payload))

    assert summary["accounts_imported"] == 2
    assert summary["points_imported"] == 2
    assert summary["meters_imported"] == 1
    assert summary["skipped"] == 1
    assert table_count("metering_meters") == 1
    assert [row["meter_number"] for row in db.get_schetchiki("SYNTH-0000")] == ["SERIAL-0000"]
    assert db.get_schetchiki("SYNTH-0001") == []
    assert any(
        "[1].MeteringPoints[0].Meters[0]" in message and DUPLICATE_METER_ID in message
        for message in import_warnings(caplog)
    )
