"""Privacy-preserving analytics events and bounded dashboard aggregates."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import database as db

log = logging.getLogger("rso.analytics")
MOSCOW = ZoneInfo("Europe/Moscow")
ALLOWED_EVENTS = frozenset({
    "faq_script_start", "faq_node_view", "faq_exit", "faq_final",
    "faq_transition_ai", "faq_transition_appeal", "faq_transition_operator",
    "operator_assigned", "operator_reassigned", "operator_connection_lost",
    "ai_question", "ai_answer_delivered", "ai_transition_operator",
    "ai_transition_appeal", "ai_limit_hit", "ai_provider_error",
})
ALLOWED_METADATA = frozenset({"reason", "error_class", "source"})


def utc_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def record_event(
    event_type: str,
    *,
    script_id: int | None = None,
    node_id: int | None = None,
    dialog_id: int | None = None,
    operator_id: int | None = None,
    metadata: dict[str, str] | None = None,
    dedupe_key: str | None = None,
    occurred_at: datetime | None = None,
) -> bool:
    """Write an allowlisted, content-free event; telemetry is always fail-open."""
    if event_type not in ALLOWED_EVENTS:
        raise ValueError("unsupported analytics event")
    clean_meta = {
        key: str(value)[:80]
        for key, value in (metadata or {}).items()
        if key in ALLOWED_METADATA and value is not None
    }
    try:
        conn = db.get_conn()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO analytics_events
                   (event_type,occurred_at,script_id,node_id,dialog_id,operator_id,
                    metadata_json,dedupe_key) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    event_type, utc_iso(occurred_at), script_id, node_id,
                    dialog_id, operator_id,
                    json.dumps(clean_meta, ensure_ascii=True, sort_keys=True), dedupe_key,
                ),
            )
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()
    except sqlite3.Error:
        log.warning("Analytics event write failed type=%s", event_type, exc_info=True)
        return False


def record_event_in_transaction(
    conn: sqlite3.Connection,
    event_type: str,
    **kwargs: Any,
) -> None:
    """Transaction-local variant for operator state changes."""
    if event_type not in ALLOWED_EVENTS:
        raise ValueError("unsupported analytics event")
    metadata = kwargs.pop("metadata", None) or {}
    clean_meta = {
        key: str(value)[:80] for key, value in metadata.items()
        if key in ALLOWED_METADATA and value is not None
    }
    try:
        conn.execute(
            """INSERT OR IGNORE INTO analytics_events
               (event_type,occurred_at,script_id,node_id,dialog_id,operator_id,
                metadata_json,dedupe_key) VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_type, utc_iso(kwargs.pop("occurred_at", None)),
                kwargs.pop("script_id", None), kwargs.pop("node_id", None),
                kwargs.pop("dialog_id", None), kwargs.pop("operator_id", None),
                json.dumps(clean_meta, ensure_ascii=True, sort_keys=True),
                kwargs.pop("dedupe_key", None),
            ),
        )
    except sqlite3.Error:
        log.warning("Analytics transaction event failed type=%s", event_type, exc_info=True)


def parse_period(date_from: str | None, date_to: str | None) -> dict[str, Any]:
    """Validate an inclusive Moscow calendar period, capped at 366 days."""
    today = datetime.now(MOSCOW).date()
    default_from = today - timedelta(days=29)
    try:
        start = date.fromisoformat(date_from) if date_from else default_from
        end = date.fromisoformat(date_to) if date_to else today
    except (TypeError, ValueError) as exc:
        raise ValueError("Даты должны быть в формате ГГГГ-ММ-ДД") from exc
    if start > end:
        raise ValueError("Начальная дата не может быть позже конечной")
    if (end - start).days > 365:
        raise ValueError("Период не может превышать 366 дней")
    start_local = datetime.combine(start, time.min, MOSCOW)
    end_local = datetime.combine(end + timedelta(days=1), time.min, MOSCOW)
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        # appeals.created_at historically has minute precision; matching that
        # representation keeps the exclusive upper boundary exact and indexed.
        "local_start": start_local.strftime("%Y-%m-%d %H:%M"),
        "local_end": end_local.strftime("%Y-%m-%d %H:%M"),
        "utc_start": start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "utc_end": end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def _groups(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _duration_stats(
    conn: sqlite3.Connection,
    *,
    table: str,
    start_column: str,
    end_column: str,
    period_column: str,
    start: str,
    end: str,
    extra_where: str = "1=1",
) -> tuple[float | None, float | None]:
    # All identifiers and extra_where are module constants, never request data.
    row = conn.execute(  # nosec B608
        f"""WITH values_ranked AS (
              SELECT (julianday({end_column})-julianday({start_column}))*86400 seconds,
                     ROW_NUMBER() OVER (ORDER BY (julianday({end_column})-julianday({start_column}))*86400) rn,
                     COUNT(*) OVER () cnt
              FROM {table}
              WHERE julianday({period_column})>=julianday(?)
                AND julianday({period_column})<julianday(?)
                AND {start_column} IS NOT NULL AND {end_column} IS NOT NULL
                AND julianday({end_column})>=julianday({start_column}) AND {extra_where}
            )
            SELECT ROUND(AVG(seconds),1) average,
                   ROUND(AVG(CASE WHEN rn IN ((cnt+1)/2,(cnt+2)/2) THEN seconds END),1) median
            FROM values_ranked""",
        (start, end),
    ).fetchone()
    return row["average"], row["median"]


def build_dashboard(period: dict[str, Any]) -> dict[str, Any]:
    """Build all dashboard sections using bounded set-based queries."""
    ls, le = period["local_start"], period["local_end"]
    us, ue = period["utc_start"], period["utc_end"]
    conn = db.get_conn()
    try:
        appeal_where = "created_at>=? AND created_at<?"
        appeal_params = (ls, le)
        total = conn.execute(
            f"SELECT COUNT(*) n FROM appeals WHERE {appeal_where}", appeal_params,
        ).fetchone()["n"]
        appeal_avg, appeal_median = _duration_stats(
            conn, table="appeals", start_column="created_at", end_column="closed_at",
            period_column="created_at", start=ls, end=le,
        )
        repeats = conn.execute(
            f"""SELECT COUNT(*) n FROM appeals a WHERE {appeal_where}
                AND EXISTS (SELECT 1 FROM appeals p WHERE p.id<a.id
                  AND p.category=a.category
                  AND ((a.chat_id IS NOT NULL AND p.chat_id=a.chat_id)
                    OR (a.chat_id IS NULL AND a.ls IS NOT NULL AND p.ls=a.ls))
                  AND julianday(p.created_at)>=julianday(a.created_at)-30
                  AND julianday(p.created_at)<=julianday(a.created_at))""",
            appeal_params,
        ).fetchone()["n"]
        appeals = {
            "total": int(total),
            "categories": _groups(conn, f"SELECT category label,COUNT(*) value FROM appeals WHERE {appeal_where} GROUP BY category ORDER BY value DESC", appeal_params),
            "sources": _groups(conn, f"SELECT COALESCE(source,'unknown/legacy') label,COUNT(*) value FROM appeals WHERE {appeal_where} GROUP BY COALESCE(source,'unknown/legacy') ORDER BY value DESC", appeal_params),
            "statuses": _groups(conn, f"SELECT status label,COUNT(*) value FROM appeals WHERE {appeal_where} GROUP BY status ORDER BY value DESC", appeal_params),
            "avg_resolution_seconds": appeal_avg,
            "median_resolution_seconds": appeal_median,
            "repeats": int(repeats),
        }

        event_params = (us, ue)
        event_where = "occurred_at>=? AND occurred_at<?"
        faq = {
            "script_starts": _groups(conn, f"""SELECT e.script_id,s.title label,COUNT(*) value FROM analytics_events e
                LEFT JOIN scripts s ON s.id=e.script_id WHERE {event_where} AND event_type='faq_script_start'
                GROUP BY e.script_id,s.title ORDER BY value DESC""", event_params),
            "node_views": _groups(conn, f"""SELECT e.node_id,n.title label,COUNT(*) value FROM analytics_events e
                LEFT JOIN script_nodes n ON n.id=e.node_id WHERE {event_where} AND event_type='faq_node_view'
                GROUP BY e.node_id,n.title ORDER BY value DESC LIMIT 100""", event_params),
            "exits": _groups(conn, f"""SELECT COALESCE(json_extract(metadata_json,'$.reason'),'unknown') label,COUNT(*) value
                FROM analytics_events WHERE {event_where} AND event_type='faq_exit' GROUP BY label ORDER BY value DESC""", event_params),
            "finals": _groups(conn, f"""SELECT e.node_id,n.title label,COUNT(*) value FROM analytics_events e
                LEFT JOIN script_nodes n ON n.id=e.node_id WHERE {event_where} AND event_type='faq_final'
                GROUP BY e.node_id,n.title ORDER BY value DESC LIMIT 100""", event_params),
            "transitions": _groups(conn, f"""SELECT replace(event_type,'faq_transition_','') label,COUNT(*) value
                FROM analytics_events WHERE {event_where} AND event_type IN
                ('faq_transition_ai','faq_transition_appeal','faq_transition_operator') GROUP BY event_type ORDER BY value DESC""", event_params),
        }

        dialog_count = conn.execute(
            """SELECT COUNT(*) n FROM operator_dialogs
               WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)""",
            (us, ue),
        ).fetchone()["n"]
        wait_avg, _ = _duration_stats(
            conn, table="operator_dialogs", start_column="created_at",
            end_column="first_assigned_at", period_column="created_at", start=us, end=ue,
        )
        duration_avg, duration_median = _duration_stats(
            conn, table="operator_dialogs", start_column="first_assigned_at",
            end_column="closed_at", period_column="created_at", start=us, end=ue,
        )
        operator = {
            "dialogs": int(dialog_count), "avg_wait_seconds": wait_avg,
            "avg_duration_seconds": duration_avg, "median_duration_seconds": duration_median,
            "by_operator": _groups(conn, """WITH participation AS (
                  SELECT DISTINCT e.dialog_id,e.operator_id
                  FROM analytics_events e JOIN operator_dialogs d ON d.id=e.dialog_id
                  WHERE e.event_type='operator_assigned' AND e.operator_id IS NOT NULL
                    AND julianday(d.created_at)>=julianday(?) AND julianday(d.created_at)<julianday(?)
                  UNION ALL
                  SELECT d.id,d.operator_id FROM operator_dialogs d
                  WHERE d.operator_id IS NOT NULL
                    AND julianday(d.created_at)>=julianday(?) AND julianday(d.created_at)<julianday(?)
                    AND NOT EXISTS (SELECT 1 FROM analytics_events e
                      WHERE e.dialog_id=d.id AND e.event_type='operator_assigned')
                )
                SELECT p.operator_id,COALESCE(u.name,'Удалённый оператор') label,
                       COUNT(DISTINCT p.dialog_id) value
                FROM participation p LEFT JOIN users u ON u.id=p.operator_id
                GROUP BY p.operator_id,u.name ORDER BY value DESC,label""", (us, ue, us, ue)),
            "legacy_operator_dialogs": conn.execute("""SELECT COUNT(*) n
                FROM operator_dialogs d WHERE d.operator_id IS NOT NULL
                  AND julianday(d.created_at)>=julianday(?) AND julianday(d.created_at)<julianday(?)
                  AND NOT EXISTS (SELECT 1 FROM analytics_events e
                    WHERE e.dialog_id=d.id AND e.event_type='operator_assigned')""", (us, ue)).fetchone()["n"],
            "reassignments": conn.execute(f"SELECT COUNT(*) n FROM analytics_events WHERE {event_where} AND event_type='operator_reassigned'", event_params).fetchone()["n"],
            "lost_connections": conn.execute(f"SELECT COUNT(*) n FROM analytics_events WHERE {event_where} AND event_type='operator_connection_lost'", event_params).fetchone()["n"],
            "ratings": _groups(conn, """SELECT rating label,COUNT(*) value FROM operator_dialogs
                WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) AND rating IS NOT NULL
                GROUP BY rating ORDER BY rating""", (us, ue)),
            "average_rating": conn.execute("""SELECT ROUND(AVG(rating),2) value FROM operator_dialogs
                WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) AND rating IS NOT NULL""", (us, ue)).fetchone()["value"],
            "confirmed_complaints": conn.execute("""SELECT COUNT(*) n FROM operator_client_reports
                WHERE status='confirmed' AND julianday(COALESCE(decided_at,created_at))>=julianday(?)
                AND julianday(COALESCE(decided_at,created_at))<julianday(?)""", (us, ue)).fetchone()["n"],
        }

        ai_counts = {row["event_type"]: int(row["n"]) for row in conn.execute(
            f"""SELECT event_type,COUNT(*) n FROM analytics_events WHERE {event_where}
                AND event_type LIKE 'ai_%' GROUP BY event_type""", event_params,
        ).fetchall()}
        ai = {
            "questions": ai_counts.get("ai_question", 0),
            "successful_answers": ai_counts.get("ai_answer_delivered", 0),
            "operator_transitions": ai_counts.get("ai_transition_operator", 0),
            "appeal_transitions": ai_counts.get("ai_transition_appeal", 0),
            "limit_hits": ai_counts.get("ai_limit_hit", 0),
            "errors": ai_counts.get("ai_provider_error", 0),
            "error_classes": _groups(conn, f"""SELECT COALESCE(json_extract(metadata_json,'$.error_class'),'unknown') label,COUNT(*) value
                FROM analytics_events WHERE {event_where} AND event_type='ai_provider_error' GROUP BY label ORDER BY value DESC""", event_params),
        }
        return {"appeals": appeals, "faq": faq, "operator": operator, "ai": ai}
    finally:
        conn.close()

