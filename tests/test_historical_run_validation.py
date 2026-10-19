import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("GEMINI_API_KEY", "milestone-test-key")

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import app as app_module
from backend import database, reports
from backend.database import initialize_database
from backend.schemas import (
    AIBugReportCreate,
    BrowserErrorCreate,
    TestRun,
    TestRunCreate,
    TestStepCreate,
)
from backend.security import API_TOKEN


def _insert_raw_historical_run(
    db_path: Path,
    *,
    run_id: str,
    target: str,
    status: str = "completed",
    run_name: str = "Historical Regression Run",
    mode: str = "user-directed",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO test_runs (
                id, run_name, platform, mode, target, goal, expected_results,
                max_steps, max_duration_seconds, status, created_at, updated_at,
                allowed_hosts, allow_form_submission, login_username_selector,
                login_password_selector, login_submit_selector, rejection_selector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run_name,
                "website",
                mode,
                target,
                "Historical test goal",
                "[\"Expected successful outcome\"]",
                20,
                300,
                status,
                "2026-10-17T10:00:00Z",
                "2026-10-17T10:05:00Z",
                "[]",
                0,
                "#user",
                "#pass",
                "#submit",
                "#error",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestHistoricalRunValidationRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_qai.db"
        initialize_database(self.db_path)
        self.client = TestClient(
            app_module.app,
            headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"},
        )
        self.patcher = patch.object(database, "DATABASE_PATH", self.db_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.client.close()
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_historical_loopback_and_private_targets_deserialize_safely(self):
        targets = [
            "http://127.0.0.1:8765/",
            "http://localhost:3000/app",
            "http://10.0.0.1:8080/dashboard",
            "http://192.168.1.50/admin",
            "http://169.254.169.254/latest/meta-data/",
        ]
        for target in targets:
            with self.subTest(target=target):
                run_id = str(uuid4())
                _insert_raw_historical_run(self.db_path, run_id=run_id, target=target)

                # Deserialization must succeed without raising ValidationError
                run = database.get_test_run(run_id, database_path=self.db_path)
                self.assertIsNotNone(run)
                self.assertIsInstance(run, TestRun)
                self.assertEqual(run.target, target)
                self.assertEqual(run.id, run_id)

    def test_historical_run_endpoints_return_200(self):
        run_id = str(uuid4())
        target = "http://127.0.0.1:8765/"
        _insert_raw_historical_run(self.db_path, run_id=run_id, target=target)

        # Add related records
        database.add_test_step(
            run_id,
            TestStepCreate(step_number=1, action="Load homepage", status="passed"),
            database_path=self.db_path,
        )
        database.add_browser_error(
            run_id,
            BrowserErrorCreate(
                step_number=1,
                category="console_error",
                message="Error on 127.0.0.1",
            ),
            database_path=self.db_path,
        )
        database.create_ai_bug_report(
            observation="Observed failure",
            report_markdown="## Bug Title\nObserved Issue",
            run_id=run_id,
            database_path=self.db_path,
        )

        # 1. GET /test-runs/{run_id}
        resp = self.client.get(f"/test-runs/{run_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["target"], target)

        # 2. GET /test-runs/{run_id}/browser-errors
        resp_errors = self.client.get(f"/test-runs/{run_id}/browser-errors")
        self.assertEqual(resp_errors.status_code, 200)
        self.assertEqual(len(resp_errors.json()), 1)

        # 3. GET /test-runs/{run_id}/ai-bug-reports
        resp_reports = self.client.get(f"/test-runs/{run_id}/ai-bug-reports")
        self.assertEqual(resp_reports.status_code, 200)
        self.assertEqual(len(resp_reports.json()), 1)

        # 4. GET /test-runs/{run_id}/reports/html
        resp_html = self.client.get(f"/test-runs/{run_id}/reports/html")
        self.assertEqual(resp_html.status_code, 200)
        html_content = resp_html.text
        self.assertIn("127.0.0.1:8765", html_content)
        # Verify it's rendered as safe text in <dd>, NOT as an active clickable link or iframe
        self.assertIn("<dt>Target</dt><dd>http://127.0.0.1:8765/</dd>", html_content)
        self.assertNotIn('<a href="http://127.0.0.1:8765/', html_content)
        self.assertNotIn('<iframe src="http://127.0.0.1:8765/', html_content)

        # 5. GET /test-runs/{run_id}/reports/excel
        resp_excel = self.client.get(f"/test-runs/{run_id}/reports/excel")
        self.assertEqual(resp_excel.status_code, 200)

    def test_ai_analysis_on_historical_private_target_succeeds(self):
        run_id = str(uuid4())
        _insert_raw_historical_run(
            self.db_path, run_id=run_id, target="http://127.0.0.1:8765/"
        )
        database.add_browser_error(
            run_id,
            BrowserErrorCreate(
                step_number=1,
                category="http_error",
                message="HTTP 500 Internal Error",
                http_status=500,
            ),
            database_path=self.db_path,
        )

        with patch.object(
            app_module.client.models,
            "generate_content",
            return_value=SimpleNamespace(text="## Bug Title\nHistorical Private Bug"),
        ):
            resp = self.client.post(f"/test-runs/{run_id}/analyze")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["title"], "Historical Private Bug")

    def test_historical_pending_run_with_private_target_cannot_be_started(self):
        run_id = str(uuid4())
        _insert_raw_historical_run(
            self.db_path,
            run_id=run_id,
            target="http://127.0.0.1:8765/",
            status="pending",
        )

        # Attempting to start execution must be blocked with 422
        resp = self.client.post(f"/test-runs/{run_id}/start")
        self.assertEqual(resp.status_code, 422)
        self.assertIn(
            "Private, loopback, and reserved IP targets are blocked",
            resp.json()["detail"],
        )

    def test_new_run_creation_strictly_blocks_private_and_loopback_targets(self):
        blocked_targets = [
            "http://127.0.0.1:8000/",
            "http://localhost:8000/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/",
            "http://metadata/",
        ]
        for target in blocked_targets:
            with self.subTest(target=target):
                # 1. Direct schema validation
                with self.assertRaises(ValidationError):
                    TestRunCreate(
                        run_name="Blocked Test",
                        platform="website",
                        mode="user-directed",
                        target=target,
                        goal="Test target",
                    )

                # 2. API endpoint validation
                resp = self.client.post(
                    "/test-runs",
                    json={
                        "run_name": "Blocked Test",
                        "platform": "website",
                        "mode": "user-directed",
                        "target": target,
                        "goal": "Test target",
                    },
                )
                self.assertEqual(resp.status_code, 422)

    def test_new_run_creation_strictly_blocks_private_allowed_hosts(self):
        with self.assertRaises(ValidationError):
            TestRunCreate(
                run_name="Blocked Host",
                platform="website",
                mode="user-directed",
                target="https://example.com/",
                goal="Test host",
                allowed_hosts=["127.0.0.1"],
            )

        resp = self.client.post(
            "/test-runs",
            json={
                "run_name": "Blocked Host",
                "platform": "website",
                "mode": "user-directed",
                "target": "https://example.com/",
                "goal": "Test host",
                "allowed_hosts": ["localhost"],
            },
        )
        self.assertEqual(resp.status_code, 422)

    def test_saved_database_records_are_never_rewritten_or_deleted(self):
        run_id = str(uuid4())
        original_target = "http://127.0.0.1:8765/original"
        _insert_raw_historical_run(
            self.db_path, run_id=run_id, target=original_target
        )

        # Perform read operations
        database.get_test_run(run_id, database_path=self.db_path)
        self.client.get(f"/test-runs/{run_id}")
        self.client.get(f"/test-runs/{run_id}/reports/html")

        # Verify raw database row is untouched
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM test_runs WHERE id = ?", (run_id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["target"], original_target)
        finally:
            conn.close()

    def test_live_workspace_database_historical_runs_load_without_error(self):
        # Verify all actual historical runs stored in backend/qai.db can be deserialized
        real_db = Path(__file__).resolve().parent.parent / "backend" / "qai.db"
        if not real_db.exists():
            self.skipTest("backend/qai.db does not exist")

        conn = sqlite3.connect(real_db)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id FROM test_runs").fetchall()
        finally:
            conn.close()

        self.assertGreater(len(rows), 0)
        for row in rows:
            run = database.get_test_run(row["id"], database_path=real_db)
            self.assertIsNotNone(run)
            self.assertIsInstance(run, TestRun)


if __name__ == "__main__":
    unittest.main()
