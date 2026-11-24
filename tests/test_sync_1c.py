from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db


class Sync1CTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "test.sqlite")
        db.init_db()
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO meter_readings(created_at,chat_id,ls,personal_account_id,"
            "room_key_id,metering_point_id,meter_id,resource,meter_number) "
            "VALUES ('2026-11-03 20:07:07',42,'100001','account-1','room-1',"
            "'point-1','meter-1','Electricity','M-1')"
        )
        reading_id = conn.execute("SELECT id FROM meter_readings").fetchone()[0]
        conn.execute(
            "INSERT INTO meter_reading_values(reading_id,scale_id,scale_name,zone_id,zone_name,value) "
            "VALUES (?, 'scale-1','Active','zone-1','Day','123.5')", (reading_id,),
        )
        conn.commit()
        conn.close()
        self.task = db.get_unsent_meter_reading_tasks(100)[0]
        self.task_id = self.task["TaskID"]
        import sync_1c
        self.sync = sync_1c

    def tearDown(self) -> None:
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_job_sends_flat_tasks_and_records_pending_then_polls_status(self) -> None:
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync.client_1c, "send_tasks", return_value=({"status": "ok", "message": ""}, None)) as send, \
             patch.object(self.sync.client_1c, "sync_task_statuses", return_value=([
                 {"TaskID": self.task_id, "Status": "PENDING", "Message": ""},
             ], None)) as poll:
            result = self.sync.sync_1c_job()

        self.assertEqual(result, "success")
        tasks = send.call_args.args[0]
        self.assertEqual(tasks[0]["TaskID"], self.task_id)
        self.assertEqual(tasks[0]["Date"], "03.11.2026 20:07:07")
        self.assertEqual(tasks[0]["Data"][0]["Value"], 123.5)
        poll.assert_called_once_with([self.task_id])
        self.assertEqual(db.list_meter_readings()[0]["status"], "PENDING")

    def test_error_status_is_saved_with_message_and_removed_from_pending_queue(self) -> None:
        db.mark_meter_readings_pending([self.task_id])
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync.client_1c, "send_tasks") as send, \
             patch.object(self.sync.client_1c, "sync_task_statuses", return_value=([
                 {"TaskID": self.task_id, "Status": "ERROR", "Message": "invalid value"},
             ], None)):
            self.assertEqual(self.sync.sync_1c_job(), "success")
        send.assert_not_called()
        reading = db.list_meter_readings()[0]
        self.assertEqual((reading["status"], reading["message"]), ("ERROR", "invalid value"))
        self.assertEqual(db.get_pending_meter_reading_task_ids(100), [])

    def test_incomplete_status_response_does_not_change_any_status(self) -> None:
        db.mark_meter_readings_pending([self.task_id])
        self.assertEqual(
            self.sync._valid_status_response([], [self.task_id]), None
        )
        self.assertEqual(db.list_meter_readings()[0]["status"], "PENDING")

    def test_batch_limit_is_applied_to_send_and_poll(self) -> None:
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync, "INTEGRATION_1C_BATCH_SIZE", 1), \
             patch.object(self.sync.client_1c, "send_tasks", return_value=({"status": "ok", "message": ""}, None)) as send, \
             patch.object(self.sync.client_1c, "sync_task_statuses", return_value=([
                 {"TaskID": self.task_id, "Status": "SUCCESS", "Message": ""},
             ], None)) as poll:
            self.assertEqual(self.sync.sync_1c_job(), "success")
        self.assertEqual(len(send.call_args.args[0]), 1)
        self.assertEqual(poll.call_args.args[0], [self.task_id])


if __name__ == "__main__":
    unittest.main()
