from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import render_template

import database as db
from web import app

ACCOUNT_ID = "10000000-0000-4000-8000-0000000000a1"
POINT_ID = "20000000-0000-4000-8000-0000000000a1"
METER_ID = "30000000-0000-4000-8000-0000000000a1"
SCALE_ID = "40000000-0000-4000-8000-0000000000a1"
DAY_ID = "50000000-0000-4000-8000-0000000000a1"
NIGHT_ID = "50000000-0000-4000-8000-0000000000a2"


class Portal1CTests(unittest.TestCase):
    def _render(self, rows: list[dict], limit: int = 1000) -> str:
        with app.test_request_context("/pokazaniya"):
            return render_template(
                "pokazaniya.html", rows=rows, limit=limit, user={"name": "Тест"},
            )

    def _row(self, status: str | None, message: str | None = None) -> dict:
        return {
            "id": 1,
            "created_at": "2026-10-27 11:23:09",
            "ls": "100001",
            "resource": "Электроэнергия",
            "meter_number": "M-1",
            "value_lines": ["Основная, День: 13800", "Основная, Ночь: 5500"],
            "status": status,
            "status_label": db._METER_READING_STATUS_LABELS[status],
            "message": message,
        }

    def test_each_1c_status_has_its_own_label(self) -> None:
        cases = {
            None: ("Не отправлено", "badge-secondary"),
            "PENDING": ("В обработке", "badge-warning"),
            "SUCCESS": ("Принято", "badge-success"),
            "ERROR": ("Ошибка", "badge-danger"),
        }
        for status, (label, badge) in cases.items():
            with self.subTest(status=status):
                html = self._render([self._row(status)])
                self.assertIn(f'<span class="badge {badge}">{label}</span>', html)
                self.assertIn("Статус 1С", html)
                self.assertNotIn("None", html)

    def test_values_message_and_columns(self) -> None:
        html = self._render([self._row("ERROR", "Прибор <снят> с учёта")])

        self.assertIn("Основная, День: 13800", html)
        self.assertIn("Основная, Ночь: 5500", html)
        self.assertIn("2026-10-27 11:23:09", html)
        self.assertIn("Прибор &lt;снят&gt; с учёта", html)  # сообщение 1С экранируется
        self.assertNotIn(">Предыдущие<", html)
        self.assertNotIn(">Разница<", html)

    def test_limit_note_is_shown_only_when_list_is_truncated(self) -> None:
        self.assertIn("Показаны последние 1", self._render([self._row(None)], limit=1))
        self.assertNotIn("Показаны последние", self._render([self._row(None)], limit=2))
        self.assertIn("Показания пока не передавались", self._render([], limit=2))

    def test_page_lists_only_new_readings_with_statuses_from_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            db, "DB_PATH", str(Path(tmp) / "portal.sqlite")
        ), patch.object(db, "BOOTSTRAP_ADMIN_PASSWORD", ""):
            db.init_db()
            db.import_from_1c_json(json.dumps([{
                "PersonalAccountID": ACCOUNT_ID,
                "PersonalAccount": "100001",
                "MeteringPoints": [{
                    "MeteringPointID": POINT_ID,
                    "Resource": "Электроэнергия",
                    "Meters": [{
                        "MeterID": METER_ID,
                        "MeterNumber": "M-1",
                        "Scales": [{"ScaleID": SCALE_ID, "Scale": "Основная", "TariffZones": [
                            {"TariffZoneID": DAY_ID, "TariffZone": "День"},
                            {"TariffZoneID": NIGHT_ID, "TariffZone": "Ночь"},
                        ]}],
                    }],
                }],
            }]))
            db.add_pokazaniya(1, "100001", "Вода", "LEGACY-METER", "10")
            ids = []
            for day, night in (("100", "50"), ("110", "55"), ("120", "60")):
                result = db.save_meter_reading(1, "100001", METER_ID, [
                    {"scale_id": SCALE_ID, "zone_id": DAY_ID, "value": day},
                    {"scale_id": SCALE_ID, "zone_id": NIGHT_ID, "value": night},
                ])
                ids.append(result["reading_id"])
            task = db.get_unsent_meter_reading_tasks(100)[0]
            db.mark_meter_readings_pending([task["TaskID"]])
            db.apply_meter_reading_task_statuses([{
                "TaskID": task["TaskID"], "Status": "ERROR", "message": "Неверное показание",
            }])
            rows = db.list_meter_readings()
            self.assertEqual(db.get_counts()["pokazaniya"], 3)

        self.assertEqual([row["id"] for row in rows], list(reversed(ids)))
        self.assertEqual(
            [row["status_label"] for row in rows], ["Ошибка", "Ошибка", "Ошибка"]
        )
        self.assertEqual(rows[2]["message"], "Неверное показание")
        self.assertEqual(rows[0]["value_lines"], ["Основная, День: 120", "Основная, Ночь: 60"])
        html = self._render(rows)
        self.assertNotIn("LEGACY-METER", html)
        self.assertIn("Основная, Ночь: 55", html)
        self.assertIn("Неверное показание", html)


if __name__ == "__main__":
    unittest.main()
