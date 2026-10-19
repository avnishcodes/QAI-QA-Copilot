import json
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import app as app_module
from backend import database
from backend.schemas import AIBugReport, AIBugReportCreate, TestRunCreate
from backend.security import API_TOKEN


class TestAIBugReportPersistenceDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_qai.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_schema_creates_ai_bug_reports_table(self):
        with database._connection(self.db_path) as conn:
            columns = {
                row["name"]: row["type"]
                for row in conn.execute("PRAGMA table_info(ai_bug_reports)").fetchall()
            }
            self.assertIn("id", columns)
            self.assertIn("run_id", columns)
            self.assertIn("observation", columns)
            self.assertIn("title", columns)
            self.assertIn("report_markdown", columns)
            self.assertIn("model", columns)
            self.assertIn("created_at", columns)

            indexes = [
                row["name"]
                for row in conn.execute("PRAGMA index_list(ai_bug_reports)").fetchall()
            ]
            self.assertTrue(any("created_at" in name for name in indexes))
            self.assertTrue(any("run_id" in name for name in indexes))

    def test_migration_preserves_existing_data(self):
        # Create a run in the database
        run = database.create_test_run(
            TestRunCreate(
                run_name="Pre-existing test run",
                platform="website",
                mode="user-directed",
                target="https://example.com/login",
                goal="Verify pre-existing data survives",
                expected_results=["Expected success"],
            ),
            database_path=self.db_path,
        )
        self.assertIsNotNone(run.id)

        # Run initialize_database again (idempotent migration)
        database.initialize_database(self.db_path)

        # Confirm run still exists
        loaded_run = database.get_test_run(run.id, database_path=self.db_path)
        self.assertIsNotNone(loaded_run)
        self.assertEqual(loaded_run.run_name, "Pre-existing test run")

    def test_create_and_get_ai_bug_report(self):
        report_text = (
            "## Bug Title\n\nLogin button remains disabled\n\n## Description\nDetailed description."
        )
        created = database.create_ai_bug_report(
            observation="When entering valid credentials, the button is disabled.",
            report_markdown=report_text,
            model="gemini-1.5-flash",
            database_path=self.db_path,
        )

        self.assertIsInstance(created, AIBugReport)
        self.assertEqual(created.title, "Login button remains disabled")
        self.assertEqual(created.model, "gemini-1.5-flash")
        self.assertIsNone(created.run_id)

        loaded = database.get_ai_bug_report(created.id, database_path=self.db_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, created.id)
        self.assertEqual(loaded.title, "Login button remains disabled")
        self.assertEqual(loaded.report_markdown, report_text)

    def test_title_extraction_fallback_when_no_header(self):
        report_text = "Some freeform markdown without the standard header."
        observation = "A short tester observation about an image crash."
        created = database.create_ai_bug_report(
            observation=observation,
            report_markdown=report_text,
            database_path=self.db_path,
        )
        self.assertEqual(created.title, observation)

    def test_optional_run_linkage(self):
        run = database.create_test_run(
            TestRunCreate(
                run_name="Run for Bug Linkage",
                platform="website",
                mode="user-directed",
                target="https://example.com/test",
                goal="Link bug to this run",
                expected_results=["Result 1"],
            ),
            database_path=self.db_path,
        )

        created = database.create_ai_bug_report(
            observation="Observed error in run",
            report_markdown="## Bug Title\n\nRun Error\n\nDetails",
            run_id=run.id,
            database_path=self.db_path,
        )
        self.assertEqual(created.run_id, run.id)

        # Retrieve report
        loaded = database.get_ai_bug_report(created.id, database_path=self.db_path)
        self.assertEqual(loaded.run_id, run.id)

    def test_reject_nonexistent_run_id(self):
        fake_id = str(uuid4())
        with self.assertRaises(ValueError) as cm:
            database.create_ai_bug_report(
                observation="Observation",
                report_markdown="## Bug Title\n\nTitle\n\nDetails",
                run_id=fake_id,
                database_path=self.db_path,
            )
        self.assertIn("not found", str(cm.exception))

    def test_run_deletion_sets_null_foreign_key(self):
        run = database.create_test_run(
            TestRunCreate(
                run_name="Run to be deleted",
                platform="website",
                mode="user-directed",
                target="https://example.com/delete-test",
                goal="Test ON DELETE SET NULL",
                expected_results=["Result 1"],
            ),
            database_path=self.db_path,
        )

        report = database.create_ai_bug_report(
            observation="Linked observation",
            report_markdown="## Bug Title\n\nDelete Link Test",
            run_id=run.id,
            database_path=self.db_path,
        )
        self.assertEqual(report.run_id, run.id)

        # Delete the test run
        with database._connection(self.db_path) as conn:
            conn.execute("DELETE FROM test_runs WHERE id = ?", (run.id,))

        # The AI bug report still exists, but run_id is now None
        loaded = database.get_ai_bug_report(report.id, database_path=self.db_path)
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.run_id)

    def test_list_ai_bug_reports_pagination_and_filter(self):
        run_a = database.create_test_run(
            TestRunCreate(
                run_name="Run A",
                platform="website",
                mode="user-directed",
                target="https://example.com/a",
                goal="Goal A",
                expected_results=["Result A"],
            ),
            database_path=self.db_path,
        )

        # Create 3 reports: 2 linked to Run A, 1 unlinked
        rep1 = database.create_ai_bug_report(
            observation="Obs 1",
            report_markdown="## Bug Title\n\nTitle 1",
            run_id=run_a.id,
            database_path=self.db_path,
        )
        rep2 = database.create_ai_bug_report(
            observation="Obs 2",
            report_markdown="## Bug Title\n\nTitle 2",
            run_id=run_a.id,
            database_path=self.db_path,
        )
        rep3 = database.create_ai_bug_report(
            observation="Obs 3",
            report_markdown="## Bug Title\n\nTitle 3",
            run_id=None,
            database_path=self.db_path,
        )

        # List all
        all_reports, total = database.list_ai_bug_reports(database_path=self.db_path)
        self.assertEqual(total, 3)
        self.assertEqual(len(all_reports), 3)

        # List linked to run_a
        run_reports, run_total = database.list_ai_bug_reports(
            run_id=run_a.id, database_path=self.db_path
        )
        self.assertEqual(run_total, 2)
        self.assertEqual(len(run_reports), 2)
        self.assertEqual({r.id for r in run_reports}, {rep1.id, rep2.id})

        # Test pagination
        page1, _ = database.list_ai_bug_reports(limit=2, offset=0, database_path=self.db_path)
        page2, _ = database.list_ai_bug_reports(limit=2, offset=2, database_path=self.db_path)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 1)


class TestAIBugReportApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api_qai.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)
        self.auth_headers = {
            "Host": "127.0.0.1:8000",
            "Authorization": f"Bearer {API_TOKEN}",
            "Origin": "http://127.0.0.1:8000",
        }

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_unauthenticated_requests_rejected(self):
        with TestClient(app_module.app, headers={"Host": "127.0.0.1:8000"}) as client:
            res_post = client.post("/ai-bug-reports", json={"observation": "Unauthenticated test"})
            self.assertEqual(res_post.status_code, 401)

            res_get = client.get("/ai-bug-reports")
            self.assertEqual(res_get.status_code, 401)

            res_get_id = client.get(f"/ai-bug-reports/{uuid4()}")
            self.assertEqual(res_get_id.status_code, 401)

            res_get_run = client.get(f"/test-runs/{uuid4()}/ai-bug-reports")
            self.assertEqual(res_get_run.status_code, 401)

    def test_post_ai_bug_reports_success(self):
        mock_report = "## Bug Title\n\nProfile image crop fails\n\n## Description\nSteps here."
        with TestClient(app_module.app, headers=self.auth_headers) as client:
            with patch.object(
                app_module.client.models,
                "generate_content",
                return_value=SimpleNamespace(text=mock_report),
            ):
                res = client.post(
                    "/ai-bug-reports",
                    json={"observation": "When I crop the profile picture, it is not saved."},
                )

        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["title"], "Profile image crop fails")
        self.assertEqual(data["report_markdown"], mock_report)
        self.assertIsNone(data["run_id"])
        self.assertIn("id", data)
        self.assertIn("created_at", data)

        # Verify saved in database
        saved = database.get_ai_bug_report(data["id"], database_path=self.db_path)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.title, "Profile image crop fails")

    def test_post_ai_bug_reports_with_run_id(self):
        run = database.create_test_run(
            TestRunCreate(
                run_name="Run for API Linkage",
                platform="website",
                mode="user-directed",
                target="https://example.com/api-link",
                goal="Test run linkage",
                expected_results=["OK"],
            ),
            database_path=self.db_path,
        )

        mock_report = "## Bug Title\n\nLinked bug\n\n## Description\nDesc."
        with TestClient(app_module.app, headers=self.auth_headers) as client:
            with patch.object(
                app_module.client.models,
                "generate_content",
                return_value=SimpleNamespace(text=mock_report),
            ):
                res = client.post(
                    "/ai-bug-reports",
                    json={
                        "observation": "Observed bug during execution",
                        "run_id": run.id,
                    },
                )

        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["run_id"], run.id)

    def test_post_ai_bug_reports_rejects_nonexistent_run_id(self):
        fake_id = str(uuid4())
        with TestClient(app_module.app, headers=self.auth_headers) as client:
            res = client.post(
                "/ai-bug-reports",
                json={
                    "observation": "Observed bug",
                    "run_id": fake_id,
                },
            )
        self.assertEqual(res.status_code, 404)
        self.assertIn("not found", res.json()["detail"])

    def test_get_ai_bug_reports_and_pagination(self):
        # Seed 3 reports
        for i in range(3):
            database.create_ai_bug_report(
                observation=f"Observation {i}",
                report_markdown=f"## Bug Title\n\nTitle {i}\n\nDetails",
                database_path=self.db_path,
            )

        with TestClient(app_module.app, headers=self.auth_headers) as client:
            res = client.get("/ai-bug-reports?limit=2&offset=0")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["total"], 3)
            self.assertEqual(len(data["items"]), 2)
            self.assertEqual(data["limit"], 2)
            self.assertEqual(data["offset"], 0)

            # Get single report
            single_id = data["items"][0]["id"]
            res_single = client.get(f"/ai-bug-reports/{single_id}")
            self.assertEqual(res_single.status_code, 200)
            self.assertEqual(res_single.json()["id"], single_id)

            # 404 on missing report
            res_404 = client.get(f"/ai-bug-reports/{uuid4()}")
            self.assertEqual(res_404.status_code, 404)

    def test_get_test_run_ai_bug_reports(self):
        run = database.create_test_run(
            TestRunCreate(
                run_name="Run for Run-Specific List",
                platform="website",
                mode="user-directed",
                target="https://example.com/specific",
                goal="Check run-specific endpoint",
                expected_results=["OK"],
            ),
            database_path=self.db_path,
        )

        rep = database.create_ai_bug_report(
            observation="Run observation",
            report_markdown="## Bug Title\n\nRun Title\n\nDetails",
            run_id=run.id,
            database_path=self.db_path,
        )

        with TestClient(app_module.app, headers=self.auth_headers) as client:
            res = client.get(f"/test-runs/{run.id}/ai-bug-reports")
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], rep.id)

            # 404 on nonexistent run
            res_404 = client.get(f"/test-runs/{uuid4()}/ai-bug-reports")
            self.assertEqual(res_404.status_code, 404)

    def test_legacy_analyze_bug_persists_to_db_and_returns_exact_legacy_shape(self):
        mock_report = "## Bug Title\n\nLegacy Bug Title\n\n## Description\nLegacy description."
        with TestClient(app_module.app, headers=self.auth_headers) as client:
            with patch.object(
                app_module.client.models,
                "generate_content",
                return_value=SimpleNamespace(text=mock_report),
            ):
                res = client.post(
                    "/analyze-bug",
                    json={"observation": "Legacy observation"},
                )

        self.assertEqual(res.status_code, 200)
        # Strict equality check matching Milestone 1 contract:
        self.assertEqual(
            res.json(),
            {"success": True, "report": mock_report},
        )

        # Confirm that the report was persisted to SQLite!
        reports, total = database.list_ai_bug_reports(database_path=self.db_path)
        self.assertEqual(total, 1)
        self.assertEqual(reports[0].title, "Legacy Bug Title")
        self.assertEqual(reports[0].report_markdown, mock_report)
        self.assertEqual(reports[0].observation, "Legacy observation")


if __name__ == "__main__":
    unittest.main()
