from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

import analytics
import database as db
import web


@pytest.fixture()
def analytics_db(monkeypatch):
    path = os.path.abspath(f".analytics-test-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def test_migration_is_additive_idempotent_and_preserves_legacy_rows(analytics_db):
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO appeals(ticket_no,channel,category,status,created_at,body)
           VALUES('RSO-20260901-0001','max','прочее','new','2026-09-01 10:00','x')"""
    )
    conn.commit()
    conn.execute("ALTER TABLE appeals DROP COLUMN source")
    conn.close()
    db.init_db()
    row = db.get_appeal_by_ticket("RSO-20260901-0001")
    assert row["source"] is None
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0] == 0
    assert "first_assigned_at" in {
        item[1] for item in conn.execute("PRAGMA table_info(operator_dialogs)")
    }
    conn.close()


def test_event_allowlist_deduplication_privacy_and_fail_open(analytics_db, monkeypatch):
    assert analytics.record_event(
        "faq_node_view", script_id=1, node_id=2,
        metadata={"reason": "menu", "raw_question": "SECRET"}, dedupe_key="same",
    )
    assert not analytics.record_event(
        "faq_node_view", script_id=1, node_id=2, dedupe_key="same",
    )
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM analytics_events").fetchone()
    conn.close()
    assert json.loads(row["metadata_json"]) == {"reason": "menu"}
    with pytest.raises(ValueError):
        analytics.record_event("arbitrary")
    monkeypatch.setattr(db, "get_conn", lambda: (_ for _ in ()).throw(sqlite3.Error("down")))
    assert analytics.record_event("ai_question") is False


def test_period_validation_uses_inclusive_moscow_days():
    period = analytics.parse_period("2026-09-01", "2026-09-02")
    assert period["local_start"] == "2026-09-01 00:00"
    assert period["local_end"] == "2026-09-03 00:00"
    assert period["utc_start"].startswith("2026-08-31T21:00:00")
    with pytest.raises(ValueError):
        analytics.parse_period("2026-09-03", "2026-09-02")
    with pytest.raises(ValueError):
        analytics.parse_period("x", "2026-09-02")
    with pytest.raises(ValueError):
        analytics.parse_period("2025-01-01", "2026-09-02")


def test_aggregates_boundaries_median_repeats_sources_and_all_sections(analytics_db):
    conn = db.get_conn()
    appeals = [
        ("A1", "100", 10, "заявка", "resolved", "2026-09-01 00:00", "2026-09-01 00:01", "bot"),
        ("A2", "100", 10, "заявка", "closed", "2026-09-02 10:00", "2026-09-02 10:03", "ai"),
        ("A3", "100", 10, "качество", "new", "2026-09-02 11:00", None, None),
        ("OUT", "100", 10, "заявка", "new", "2026-09-03 00:00", None, "bot"),
    ]
    conn.executemany(
        """INSERT INTO appeals(ticket_no,ls,chat_id,channel,category,status,body,
           created_at,closed_at,source) VALUES(?,?,?,'max',?,?,'x',?,?,?)""", appeals,
    )
    conn.execute("INSERT INTO users(id,username,password,name,role) VALUES(7,'op','x','Operator <x>','operator')")
    conn.execute(
        """INSERT INTO operator_dialogs(id,chat_id,operator_id,status,created_at,
           first_assigned_at,assigned_at,last_activity_at,closed_at,rating)
           VALUES(1,99,7,'closed','2026-09-01T00:00:00+00:00',
           '2026-09-01T00:00:10+00:00','2026-09-01T00:00:10+00:00',
           '2026-09-01T00:01:00+00:00','2026-09-01T00:02:10+00:00',5)"""
    )
    conn.execute(
        """INSERT INTO operator_client_reports(dialog_id,operator_id,client_chat_id,
           reason,snapshot_json,status,created_at) VALUES(1,7,99,'spam','[]','confirmed',
           '2026-09-01T00:03:00+00:00')"""
    )
    conn.commit()
    conn.close()
    for event in (
        "faq_script_start", "faq_node_view", "faq_final", "faq_transition_ai",
        "operator_reassigned", "operator_connection_lost", "ai_question",
        "ai_answer_delivered", "ai_transition_operator", "ai_transition_appeal",
        "ai_limit_hit", "ai_provider_error",
    ):
        analytics.record_event(
            event, script_id=1, node_id=2, dialog_id=1,
            metadata={"error_class": "provider"},
            occurred_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        )
    report = analytics.build_dashboard(analytics.parse_period("2026-09-01", "2026-09-02"))
    assert report["appeals"]["total"] == 3
    assert report["appeals"]["repeats"] == 1
    assert report["appeals"]["avg_resolution_seconds"] == 120.0
    assert report["appeals"]["median_resolution_seconds"] == 120.0
    assert {x["label"] for x in report["appeals"]["sources"]} == {"bot", "ai", "unknown/legacy"}
    assert report["faq"]["script_starts"][0]["value"] == 1
    assert report["operator"]["avg_wait_seconds"] == 10.0
    assert report["operator"]["median_duration_seconds"] == 120.0
    assert report["operator"]["confirmed_complaints"] == 1
    assert report["operator"]["legacy_operator_dialogs"] == 1
    assert report["ai"]["questions"] == 1
    assert report["ai"]["successful_answers"] == 1
    assert report["ai"]["errors"] == 1


def test_operator_participation_counts_initial_and_reassigned_without_duplicates(analytics_db):
    conn = db.get_conn()
    conn.executemany(
        "INSERT INTO users(id,username,password,name,role) VALUES(?,?,?,?, 'operator')",
        [(11, "first", "x", "Первый"), (12, "second", "x", "Второй")],
    )
    conn.execute(
        """INSERT INTO operator_dialogs(id,chat_id,operator_id,status,created_at,
           assigned_at,first_assigned_at,last_activity_at,closed_at)
           VALUES(7,77,12,'closed','2026-09-01T00:00:00+00:00',
           '2026-09-01T00:02:00+00:00','2026-09-01T00:00:10+00:00',
           '2026-09-01T00:03:00+00:00','2026-09-01T00:04:00+00:00')"""
    )
    conn.commit()
    conn.close()
    when = datetime(2026, 9, 1, 0, 0, 10, tzinfo=timezone.utc)
    assert analytics.record_event(
        "operator_assigned", dialog_id=7, operator_id=11,
        dedupe_key="operator:7:assigned:1", occurred_at=when,
    )
    assert not analytics.record_event(
        "operator_assigned", dialog_id=7, operator_id=11,
        dedupe_key="operator:7:assigned:1", occurred_at=when,
    )
    assert analytics.record_event(
        "operator_assigned", dialog_id=7, operator_id=12,
        dedupe_key="operator:7:assigned:2", occurred_at=when,
    )
    report = analytics.build_dashboard(
        analytics.parse_period("2026-09-01", "2026-09-01")
    )
    assert {(row["operator_id"], row["value"]) for row in report["operator"]["by_operator"]} == {
        (11, 1), (12, 1),
    }
    assert report["operator"]["legacy_operator_dialogs"] == 0


def _login(client, username: str, password: str) -> None:
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 302


def test_dashboard_admin_only_filters_empty_state_and_escaping(analytics_db):
    assert db.create_user("admin2", "a-very-long-password", "Admin", "admin")[0]
    assert db.create_user("operator2", "a-very-long-password", "Operator", "operator")[0]
    client = web.app.test_client()
    assert client.get("/analytics").status_code == 302
    _login(client, "operator2", "a-very-long-password")
    response = client.get("/analytics")
    assert response.status_code == 302
    assert b"analytics" not in response.data.lower()
    client.get("/logout")
    _login(client, "admin2", "a-very-long-password")
    response = client.get("/analytics?from=2026-09-01&to=2026-09-02")
    assert response.status_code == 200
    assert "За выбранный период данных нет" in response.get_data(as_text=True)
    assert client.get("/analytics?from=bad&to=2026-09-02").status_code == 400
    conn = db.get_conn()
    conn.execute("INSERT INTO scripts(title) VALUES('<script>alert(1)</script>')")
    script_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    analytics.record_event(
        "faq_script_start", script_id=script_id,
        occurred_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )
    body = client.get("/analytics?from=2026-09-01&to=2026-09-02").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body

