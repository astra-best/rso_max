from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import client_1c
import database as db
import sync_1c


class LocalIntegrationE2ETests(unittest.TestCase):
    """SQLite -> scheduled task exchange -> mock HTTP 1C -> SQLite."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "integration.sqlite")
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
            "VALUES (?, 'scale-1','Active','zone-1','Day','15.5')", (reading_id,),
        )
        conn.commit()
        conn.close()
        self.task_id = db.get_unsent_meter_reading_tasks(100)[0]["TaskID"]
        self.requests: list[dict] = []

    def tearDown(self) -> None:
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_sent_then_sync_status_round_trip(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.requests.append({"url": str(request.url), "body": body})
            if request.url.path.endswith("/RegistrationMetersValue"):
                return httpx.Response(200, json={"status": "ok", "message": ""}, request=request)
            return httpx.Response(200, json=[
                {"TaskID": task["TaskID"], "Status": "ERROR", "message": "Rejected"}
                for task in body
            ], request=request)

        transport = httpx.MockTransport(handler)

        def post(url, **kwargs):
            with httpx.Client(transport=transport) as http:
                return http.post(url, **kwargs)

        with (
            patch.object(sync_1c, "ENABLE_1C_INTEGRATION", True),
            patch.object(client_1c, "INTEGRATION_1C_MOCK", False),
            patch.object(client_1c, "INTEGRATION_1C_BASE_URL", "https://one-c.invalid/api"),
            patch.object(client_1c, "INTEGRATION_1C_AUTH_TOKEN", "test-token"),
            patch.object(client_1c.httpx, "post", side_effect=post),
        ):
            self.assertEqual(sync_1c.sync_1c_job(), "success")

        self.assertEqual(
            [request["url"].rsplit("/", 1)[-1] for request in self.requests],
            ["RegistrationMetersValue", "MetersValueStatus"],
        )
        sent_task = self.requests[0]["body"][0]
        self.assertEqual(sent_task["TaskID"], self.task_id)
        self.assertEqual(sent_task["Date"], "03.11.2026 20:07:07")
        self.assertEqual(sent_task["Data"][0]["Value"], 15.5)
        self.assertEqual(self.requests[1]["body"], [{"TaskID": self.task_id}])
        result = db.list_meter_readings()[0]
        self.assertEqual((result["status"], result["message"]), ("ERROR", "Rejected"))


if __name__ == "__main__":
    unittest.main()
