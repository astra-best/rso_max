from __future__ import annotations

import json
import sqlite3

import pytest

import database as db
from scripts.generate_synthetic_1c_json import build_payload


@pytest.fixture
def json_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "json-import.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    return db


def meter(
    meter_id: str = "METER-1",
    *,
    meter_number: str = "SN-001",
    tariff_zones: int = 2,
) -> dict:
    return {
        "MeterID": meter_id,
        "MeterNumber": meter_number,
        "DifferentiationTypeID": "TYPE-2T",
        "DifferentiationType": "Однотарифный" if tariff_zones == 1 else "Двухтарифный",
        "Scales": [
            {
                "ScaleID": "SCALE-1",
                "Scale": "Основная",
                "TariffZones": [
                    {"TariffZoneID": f"ZONE-{index}", "TariffZone": f"Тариф {index}"}
                    for index in range(1, tariff_zones + 1)
                ],
            }
        ],
    }


def point(
    point_id: str = "POINT-1",
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
        "PersonalAccount": number,
        "MeteringPoints": points or [],
        **fields,
    }


def test_import_stores_nested_entities_and_shared_point_without_readings(json_db):
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

    db.import_from_1c_json(
        json.dumps(
            [
                account("100-1", "ACCOUNT-1", points=[point(meters=[meter()])]),
                account("100-2", "ACCOUNT-2", points=[point(meters=[])]),
                account(
                    "100-3",
                    "ACCOUNT-3",
                    points=[
                        point(
                            "POINT-3",
                            meters=[
                                meter(
                                    "METER-3",
                                    meter_number="SN-003",
                                    tariff_zones=1,
                                )
                            ],
                        )
                    ],
                ),
                account(
                    "100-4",
                    "ACCOUNT-4",
                    points=[
                        point(
                            "POINT-4",
                            meters=[
                                meter(
                                    "METER-4",
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
    assert existing_account["one_c_id"] == "ACCOUNT-1"
    assert isinstance(existing_account["id"], int)
    assert [row["meter_number"] for row in db.get_schetchiki("100-1")] == [
        "SN-001",
        "LEGACY-1",
    ]
    assert [row["meter_number"] for row in db.get_schetchiki("100-2")] == ["SN-001"]
    assert db.get_schetchiki("100-1")[0]["meter_type"] == "Двухтарифный"
    assert db.get_schetchiki("100-3")[0]["meter_type"] == "Однотарифный"
    assert db.get_schetchiki("100-4")[0]["meter_type"] == "Двухтарифный"
    assert len(db.get_pokazaniya()) == readings_before

    connection = db.get_conn()
    try:
        point_row = connection.execute(
            "SELECT id, one_c_id FROM metering_points WHERE one_c_id='POINT-1'"
        ).fetchone()
        meter_row = connection.execute(
            "SELECT id, one_c_id, metering_point_id FROM metering_meters "
            "WHERE one_c_id='METER-1'"
        ).fetchone()
        assert isinstance(point_row["id"], int)
        assert point_row["id"] != point_row["one_c_id"]
        assert isinstance(meter_row["id"], int)
        assert meter_row["id"] != meter_row["one_c_id"]
        assert meter_row["metering_point_id"] == point_row["id"]
        assert connection.execute(
            "SELECT COUNT(*) FROM licschet_metering_points"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM licschet_metering_points "
            "WHERE metering_point_id=?",
            (point_row["id"],),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_meter_scales WHERE meter_id=?",
            (meter_row["id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_tariff_zones"
        ).fetchone()[0] == 6
    finally:
        connection.close()


def test_import_skips_empty_and_duplicate_accounts_before_meter_validation(json_db):
    shared_meter = meter("METER-ON-SHARED-POINT")
    db.import_from_1c_json(
        json.dumps(
            [
                account("   ", "EMPTY-ACCOUNT", points=[point(meters=[shared_meter])]),
                account("100-1", "ACCOUNT-1", points=[point(meters=[shared_meter])]),
                account(
                    " 100-1 ",
                    "DUPLICATE-ACCOUNT",
                    points=[point("IGNORED-POINT", meters=[shared_meter])],
                ),
                account("100-2", "ACCOUNT-2", points=[point(meters=[])]),
            ]
        )
    )

    connection = db.get_conn()
    try:
        assert [row[0] for row in connection.execute(
            "SELECT number FROM licschet ORDER BY number"
        )] == ["100-1", "100-2"]
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_meters"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM metering_points WHERE one_c_id='IGNORED-POINT'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM licschet_metering_points"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_repeated_meter_id_rejects_entire_import_without_mutations(json_db):
    db.create_lschet("100-1", "Original", "Original address")
    payload = [
        account("100-1", "ACCOUNT-1-UPDATED", fio="Changed"),
        account("100-2", "ACCOUNT-2", points=[point(meters=[meter("DUPLICATE")])]),
        account("100-3", "ACCOUNT-3", points=[point("POINT-3", meters=[meter("DUPLICATE")])]),
    ]

    with pytest.raises(ValueError, match="MeterID"):
        db.import_from_1c_json(json.dumps(payload))

    assert db.get_ls("100-1")["fio"] == "Original"
    assert db.get_ls("100-2") is None
    connection = db.get_conn()
    try:
        assert connection.execute("SELECT COUNT(*) FROM metering_points").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM metering_meters").fetchone()[0] == 0
    finally:
        connection.close()


def test_repeated_personal_account_id_rejects_import_before_mutation(json_db):
    db.create_lschet("100-1", "Original", "Original address")
    payload = [
        account("100-1", "SAME-ACCOUNT-ID", fio="Changed"),
        account("100-2", " SAME-ACCOUNT-ID "),
    ]

    with pytest.raises(ValueError, match="PersonalAccountID"):
        db.import_from_1c_json(json.dumps(payload))

    connection = db.get_conn()
    try:
        assert connection.execute("SELECT COUNT(*) FROM licschet").fetchone()[0] == 1
    finally:
        connection.close()
    assert db.get_ls("100-1")["fio"] == "Original"
    assert db.get_ls("100-1")["one_c_id"] is None
    assert db.get_ls("100-2") is None


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
                account(
                    "100-1",
                    "ACCOUNT-1",
                    points=[point(meters=[meter("METER-1")])],
                    FIO="Собственник",
                    Address="Адрес",
                ),
                account("100-2", "ACCOUNT-2"),
            ]
        )
    )
    db.import_from_1c_json(json.dumps([account("100-1", "ACCOUNT-1")]))

    assert db.get_ls("100-1")["fio"] == "Собственник"
    assert db.get_ls("100-1")["address"] == "Адрес"
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
        assert connection.execute("SELECT id FROM pokazaniya WHERE id=31").fetchone()[0] == 31
    finally:
        connection.close()


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
                (17, "100-1", "Owner 1", "Address 1", "LEGACY-DUPLICATE"),
                (18, "100-2", "Owner 2", "Address 2", "LEGACY-DUPLICATE"),
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

    with pytest.raises(ValueError, match="PersonalAccountID"):
        db.import_from_1c_json(
            json.dumps([account("100-3", "LEGACY-DUPLICATE")])
        )
    assert db.get_ls("100-1")["fio"] == "Owner 1"
    assert db.get_ls("100-2")["fio"] == "Owner 2"
    assert db.get_ls("100-3") is None


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


def test_synthetic_duplicate_mode_exercises_atomic_rollback(json_db):
    payload = build_payload(2, duplicate_meter_id=True)

    with pytest.raises(ValueError, match="MeterID"):
        db.import_from_1c_json(json.dumps(payload))

    connection = db.get_conn()
    try:
        assert connection.execute("SELECT COUNT(*) FROM licschet").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM metering_points").fetchone()[0] == 0
    finally:
        connection.close()
