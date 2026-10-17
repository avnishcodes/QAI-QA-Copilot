import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "milestone-test-key")

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import database
from backend.app import app, client as gemini_client
from backend.security import API_TOKEN
from backend.schemas import (
    BugRecordCreate,
    TestRunCreate,
    TestStepCreate,
)


class TestSchemaValidation(unittest.TestCase):
    def test_run_schema_defaults_and_rejects_invalid_values(self):
        run = TestRunCreate(
            run_name=" Login check ",
            platform="website",
            mode="user-directed",
            target="https://example.test/login",
            goal="Check the login form.",
        )

        self.assertEqual(run.run_name, "Login check")
        self.assertEqual(run.expected_results, [])
        self.assertEqual(run.max_steps, 20)
        self.assertEqual(run.max_duration_seconds, 300)

        with self.assertRaises(ValidationError):
            TestRunCreate(
                run_name="",
                platform="website",
                mode="unbounded",
                target="https://example.test",
                goal="Explore.",
                max_steps=0,
            )

    def test_step_and_bug_schemas_validate_values(self):
        step = TestStepCreate(step_number=1, action="Open login page")
        bug = BugRecordCreate(
            title="Login error is missing",
            description="No error message appeared after invalid credentials.",
            severity="Medium",
            confidence=0.8,
            reproduction_steps=["Submit invalid credentials"],
        )

        self.assertEqual(step.status, "pending")
        self.assertEqual(bug.reproduction_steps, ["Submit invalid credentials"])

        with self.assertRaises(ValidationError):
            BugRecordCreate(
                title="Invalid confidence",
                description="Confidence must be within [0, 1].",
                severity="High",
                confidence=1.1,
            )


class TestDatabaseOperations(unittest.TestCase):
    def test_run_status_steps_and_bug_records_round_trip(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "qai-test.db"
            database.initialize_database(database_path)
            database.initialize_database(database_path)

            run = database.create_test_run(
                TestRunCreate(
                    run_name="Login test",
                    platform="website",
                    mode="user-directed",
                    target="https://example.test/login",
                    goal="Check the invalid-login error.",
                    expected_results=["An error is displayed."],
                ),
                database_path,
            )
            self.assertEqual(run.status, "pending")
            self.assertEqual(database.get_test_run(run.id, database_path), run)

            running = database.update_test_run_status(
                run.id, "running", database_path
            )
            self.assertIsNotNone(running)
            self.assertEqual(running.status, "running")

            step = database.add_test_step(
                run.id,
                TestStepCreate(
                    step_number=1,
                    action="Submit invalid credentials",
                    expected_result="An error is displayed.",
                    actual_result="No error was displayed.",
                    status="failed",
                ),
                database_path,
            )
            self.assertEqual(database.list_test_steps(run.id, database_path), [step])

            bug = database.add_bug_record(
                run.id,
                BugRecordCreate(
                    title="Login error is missing",
                    description="Invalid credentials did not display an error.",
                    severity="Medium",
                    confidence=0.8,
                    reproduction_steps=["Submit invalid credentials"],
                    expected_result="An error is displayed.",
                    actual_result="No error was displayed.",
                ),
                database_path,
            )
            self.assertEqual(database.list_bug_records(run.id, database_path), [bug])
            self.assertIsNone(database.get_test_run("missing", database_path))
            self.assertIsNone(database.list_test_steps("missing", database_path))
            self.assertIsNone(database.list_bug_records("missing", database_path))

            with self.assertRaises(ValueError):
                database.update_test_run_status(
                    run.id, "pending", database_path
                )


class TestRunApi(unittest.TestCase):
    def test_create_status_and_steps_endpoints(self):
        with TemporaryDirectory() as temporary_directory:
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "api-test.db"
            try:
                with TestClient(app, headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"}) as client:
                    created = client.post(
                        "/test-runs",
                        json={
                            "run_name": "Login test",
                            "platform": "website",
                            "mode": "user-directed",
                            "target": "https://example.test/login",
                            "goal": "Check the invalid-login error.",
                        },
                    )

                    self.assertEqual(created.status_code, 201)
                    run = created.json()
                    self.assertEqual(run["status"], "pending")

                    status = client.get(f"/test-runs/{run['id']}")
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json()["id"], run["id"])

                    steps = client.get(f"/test-runs/{run['id']}/steps")
                    self.assertEqual(steps.status_code, 200)
                    self.assertEqual(steps.json(), [])

                    missing_run = client.get("/test-runs/missing/steps")
                    self.assertEqual(missing_run.status_code, 404)

                    with patch.object(
                        gemini_client.models,
                        "generate_content",
                        return_value=SimpleNamespace(text="Existing text report."),
                    ) as generate_content:
                        analysis = client.post(
                            "/analyze-bug",
                            json={"observation": "The save button does not work."},
                        )

                    self.assertEqual(analysis.status_code, 200)
                    self.assertEqual(
                        analysis.json(),
                        {"success": True, "report": "Existing text report."},
                    )
                    generate_content.assert_called_once()
            finally:
                database.DATABASE_PATH = original_database_path


if __name__ == "__main__":
    unittest.main()
