"""Scheduled sending of meter-reading tasks and polling their 1C statuses."""
from __future__ import annotations

import logging

import client_1c
import database as db
from config import ENABLE_1C_INTEGRATION, INTEGRATION_1C_BATCH_SIZE

log = logging.getLogger("rso.sync_1c")


def _valid_ack(result: dict | None) -> bool:
    return (
        isinstance(result, dict)
        and isinstance(result.get("status"), str)
        and result.get("status") in {"ok", "error"}
        and isinstance(result.get("message"), str)
        and ((result["status"] == "ok" and result["message"] == "")
             or (result["status"] == "error" and bool(result["message"])))
    )


def _valid_status_response(result: list | None, task_ids: list[str]) -> list[dict] | None:
    if not isinstance(result, list):
        return None
    statuses = result
    seen = set()
    for item in statuses:
        if not isinstance(item, dict):
            return None
        task_id, status, message = item.get("TaskID"), item.get("Status"), item.get("message")
        if (
            not isinstance(task_id, str) or task_id not in task_ids or task_id in seen
            or not isinstance(status, str)
            or status not in {"PENDING", "SUCCESS", "ERROR"}
            or not isinstance(message, str)
            or (status == "ERROR" and not message)
            or (status != "ERROR" and message)
        ):
            return None
        seen.add(task_id)
    if seen != set(task_ids):
        return None
    return statuses


def sync_1c_job() -> str:
    """Send a bounded batch, then refresh statuses of pending tasks."""
    if not ENABLE_1C_INTEGRATION:
        return "disabled"

    outcomes = []
    tasks = db.get_unsent_meter_reading_tasks(INTEGRATION_1C_BATCH_SIZE)
    if tasks:
        response, error = client_1c.send_tasks(tasks)
        if error or not _valid_ack(response):
            log.warning("1C task batch send failed: error=%s", error or "invalid_response")
            outcomes.append("send_failed")
        elif response["status"] == "error":
            log.error("1C rejected task batch: %s", response["message"])
            outcomes.append("send_failed")
        else:
            db.mark_meter_readings_pending([task["TaskID"] for task in tasks])
            outcomes.append("sent")
            log.info("1C accepted task batch: count=%d", len(tasks))

    task_ids = db.get_pending_meter_reading_task_ids(INTEGRATION_1C_BATCH_SIZE)
    if task_ids:
        response, error = client_1c.sync_task_statuses(task_ids)
        statuses = None if error else _valid_status_response(response, task_ids)
        if statuses is None:
            log.warning("1C task status sync failed: error=%s", error or "invalid_response")
            outcomes.append("status_failed")
        else:
            db.apply_meter_reading_task_statuses(statuses)
            outcomes.append("status_updated")
            log.info("1C task statuses updated: count=%d", len(statuses))

    if not outcomes:
        return "not_due"
    if any(item.endswith("failed") for item in outcomes):
        return "failed"
    return "success"
