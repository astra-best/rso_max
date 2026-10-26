from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import render_template

import database as db
from web import app


class Portal1CTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "id": 1,
            "ls": "100001",
            "resource_type": "Электроэнергия",
            "meter_number": "M-1",
            "value1": "10",
            "value2": None,
            "prev_value1": "9",
            "prev_value2": None,
            "diff1": 1,
            "diff2": None,
            "created_at": "2026-08-19 12:00",
            "status_1c": "accepted",
        }

    def _render(self, enabled: bool, rows: list[dict] | None = None) -> str:
        with app.test_request_context("/pokazaniya"):
            return render_template(
                "pokazaniya.html",
                rows=[self.row] if rows is None else rows,
                user={"name": "Тест"},
                integration_1c_enabled=enabled,
            )

    def test_integration_mode_shows_1c_status_without_previous_columns(self) -> None:
        html = self._render(True)
        self.assertIn("Статус 1С", html)
        self.assertIn("Принято", html)
        self.assertNotIn(">Предыдущие<", html)
        self.assertNotIn(">Разница<", html)

    def test_legacy_mode_keeps_previous_and_difference_columns(self) -> None:
        html = self._render(False)
        self.assertIn(">Предыдущие<", html)
        self.assertIn(">Разница<", html)
        self.assertNotIn("Статус 1С", html)

    def test_new_and_legacy_readings_render_from_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            db, "DB_PATH", str(Path(tmp) / "portal.sqlite")
        ), patch.object(db, "BOOTSTRAP_ADMIN_PASSWORD", ""):
            db.init_db()
            db.add_pokazaniya(1, "100001", "Вода", "W-1", "10")
            db.add_pokazaniya(1, "100001", "Вода", "W-1", "15", "7")
            db.add_pokazaniya_values(
                1, "100001", "Вода", "W-1", "METER-1",
                [
                    {"scale_one_c_id": "S", "scale_name": "Актив",
                     "zone_one_c_id": "Z1", "zone_name": "День", "value": "123"},
                    {"scale_one_c_id": "S", "scale_name": "Актив",
                     "zone_one_c_id": "Z2", "zone_name": "Ночь", "value": "45"},
                ],
            )
            # The new-format row of the same meter must not break the legacy chain.
            db.add_pokazaniya(1, "100001", "Вода", "W-1", "20")
            rows = db.get_pokazaniya_with_prev()

        new_row = next(row for row in rows if row["meter_one_c_id"])
        self.assertEqual(new_row["values_text"], "Актив, День: 123; Актив, Ночь: 45")
        self.assertIsNone(new_row["value1"])
        self.assertEqual((rows[0]["prev_value1"], rows[0]["diff1"]), ("15", 5.0))
        for enabled in (False, True):
            html = self._render(enabled, rows)
            self.assertIn("Актив, День: 123", html)
            self.assertIn("Актив, Ночь: 45", html)
            self.assertNotIn("None", html)
            self.assertIn(">20<", html)
            self.assertIn("Т2:", html)


if __name__ == "__main__":
    unittest.main()
