import os
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend import app as app_module
from backend import database, website_runner
from backend.reports import generate_excel_report, generate_html_report
from backend.schemas import (
    BrowserError,
    BrowserErrorCategory,
    BrowserErrorCreate,
    TestRunCreate,
)
from backend.security import API_TOKEN
from backend.website_runner import (
    BrowserErrorCollector,
    sanitize_browser_url,
)


def _make_test_run(db_path: Path, **kwargs) -> database.TestRun:
    payload = {
        "run_name": "Browser Error Test Run",
        "platform": "website",
        "mode": "user-directed",
        "target": "https://example.com/login",
        "goal": "Verify error capture without side-effects",
        "max_steps": 10,
        "max_duration_seconds": 60,
        "expected_results": ["Login page loads"],
        "allowed_hosts": [],
    }
    payload.update(kwargs)
    create_req = TestRunCreate(**payload)
    return database.create_test_run(create_req, database_path=db_path)


class TestUrlSanitization(unittest.TestCase):
    def test_strips_credentials_from_url(self):
        url = "https://admin:supersecret@example.com/dashboard"
        sanitized = sanitize_browser_url(url)
        self.assertEqual(sanitized, "https://example.com/dashboard")
        self.assertNotIn("admin", sanitized)
        self.assertNotIn("supersecret", sanitized)

    def test_preserves_custom_port(self):
        url = "http://dev:pass@localhost:8080/api/v1/resource"
        sanitized = sanitize_browser_url(url)
        self.assertEqual(sanitized, "http://localhost:8080/api/v1/resource")

    def test_redacts_sensitive_query_parameters(self):
        url = (
            "https://example.com/callback?"
            "token=abc123secret&"
            "page=2&"
            "api_key=mykey99&"
            "password=myPass&"
            "auth_token=jwtXYZ&"
            "sort=desc"
        )
        sanitized = sanitize_browser_url(url)
        self.assertIn("token=%5BREDACTED%5D", sanitized)
        self.assertIn("api_key=%5BREDACTED%5D", sanitized)
        self.assertIn("password=%5BREDACTED%5D", sanitized)
        self.assertIn("auth_token=%5BREDACTED%5D", sanitized)
        self.assertIn("page=2", sanitized)
        self.assertIn("sort=desc", sanitized)
        self.assertNotIn("abc123secret", sanitized)
        self.assertNotIn("mykey99", sanitized)
        self.assertNotIn("myPass", sanitized)
        self.assertNotIn("jwtXYZ", sanitized)

    def test_strips_fragments(self):
        url = "https://example.com/home#access_token=sensitive_token"
        sanitized = sanitize_browser_url(url)
        self.assertEqual(sanitized, "https://example.com/home")
        self.assertNotIn("access_token", sanitized)

    def test_handles_edge_cases(self):
        self.assertIsNone(sanitize_browser_url(None))
        self.assertEqual(sanitize_browser_url(""), "")
        self.assertEqual(sanitize_browser_url("   "), "")
        self.assertEqual(sanitize_browser_url("about:blank"), "about:blank")
        data_url = "data:image/png;base64," + "A" * 200
        sanitized_data = sanitize_browser_url(data_url)
        self.assertTrue(len(sanitized_data) <= 100)
        self.assertTrue(sanitized_data.endswith("..."))


class TestDatabaseBrowserErrors(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_browser_db.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_browser_errors_table_schema(self):
        with database._connection(self.db_path) as conn:
            columns = {
                row["name"]: row["type"]
                for row in conn.execute("PRAGMA table_info(browser_errors)").fetchall()
            }
            self.assertIn("id", columns)
            self.assertIn("run_id", columns)
            self.assertIn("step_number", columns)
            self.assertIn("category", columns)
            self.assertIn("message", columns)
            self.assertIn("url", columns)
            self.assertIn("http_status", columns)
            self.assertIn("resource_type", columns)
            self.assertIn("timestamp", columns)

    def test_add_and_list_browser_errors(self):
        run = _make_test_run(self.db_path)
        err = database.add_browser_error(
            run.id,
            BrowserErrorCreate(
                step_number=2,
                category="page_error",
                message="Uncaught ReferenceError: foo is not defined",
                url="https://example.com/page",
                http_status=None,
                resource_type=None,
            ),
            database_path=self.db_path,
        )
        self.assertIsInstance(err, BrowserError)
        self.assertEqual(err.run_id, run.id)
        self.assertEqual(err.step_number, 2)
        self.assertEqual(err.category, "page_error")
        self.assertEqual(err.message, "Uncaught ReferenceError: foo is not defined")

        errors = database.list_browser_errors(run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, err.id)

    def test_nonexistent_run_raises_value_error(self):
        with self.assertRaises(ValueError):
            database.add_browser_error(
                str(uuid4()),
                BrowserErrorCreate(
                    category="console_error",
                    message="Console failed",
                ),
                database_path=self.db_path,
            )

    def test_cascade_delete_removes_browser_errors(self):
        run = _make_test_run(self.db_path)
        database.add_browser_error(
            run.id,
            BrowserErrorCreate(
                category="http_error",
                message="HTTP 404 Not Found",
                http_status=404,
            ),
            database_path=self.db_path,
        )
        self.assertEqual(len(database.list_browser_errors(run.id, database_path=self.db_path)), 1)

        with database._connection(self.db_path) as conn:
            conn.execute("DELETE FROM test_runs WHERE id = ?", (run.id,))

        self.assertEqual(len(database.list_browser_errors(run.id, database_path=self.db_path)), 0)

    def test_report_data_includes_browser_errors(self):
        run = _make_test_run(self.db_path)
        database.add_browser_error(
            run.id,
            BrowserErrorCreate(
                category="request_failed",
                message="Request failed: net::ERR_CONNECTION_REFUSED",
                url="https://example.com/api/status",
                resource_type="xhr",
            ),
            database_path=self.db_path,
        )
        report_data = database.get_test_run_report_data(run.id, database_path=self.db_path)
        self.assertIsNotNone(report_data)
        self.assertIn("browser_errors", report_data)
        self.assertEqual(len(report_data["browser_errors"]), 1)
        self.assertEqual(report_data["browser_errors"][0].category, "request_failed")


class FakePlaywrightEventPage:
    def __init__(self, url="https://example.com/app"):
        self.url = url
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args, **kwargs):
        for handler in self.handlers.get(event, []):
            handler(*args, **kwargs)


class TestBrowserErrorCollector(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_collector.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)
        self.run = _make_test_run(self.db_path)
        self.cancel_event = threading.Event()
        self.current_step = 1

        self.collector = BrowserErrorCollector(
            run_id=self.run.id,
            cancel_event=self.cancel_event,
            step_getter=lambda: self.current_step,
            database_path=self.db_path,
        )
        self.page = FakePlaywrightEventPage()
        self.collector.attach_to_page(self.page)

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_captures_page_error(self):
        self.page.emit("pageerror", Exception("Uncaught TypeError: null has no properties"))
        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, "page_error")
        self.assertIn("Uncaught TypeError", errors[0].message)
        self.assertEqual(errors[0].step_number, 1)
        self.assertEqual(errors[0].url, "https://example.com/app")

    def test_captures_console_error_and_ignores_log(self):
        # Console log should be ignored
        log_msg = SimpleNamespace(type="log", text="Standard debug info", location={})
        self.page.emit("console", log_msg)

        # Console warn should be ignored
        warn_msg = SimpleNamespace(type="warn", text="Deprecation warning", location={})
        self.page.emit("console", warn_msg)

        # Console error should be recorded
        error_msg = SimpleNamespace(
            type="error",
            text="Failed to load module script",
            location={"url": "https://example.com/app/main.js"},
        )
        self.page.emit("console", error_msg)

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, "console_error")
        self.assertEqual(errors[0].message, "Failed to load module script")
        self.assertEqual(errors[0].url, "https://example.com/app/main.js")

    def test_captures_http_4xx_and_5xx_and_ignores_2xx_3xx(self):
        req_200 = SimpleNamespace(resource_type="document")
        resp_200 = SimpleNamespace(status=200, status_text="OK", url="https://example.com/ok", request=req_200)
        self.page.emit("response", resp_200)

        req_302 = SimpleNamespace(resource_type="document")
        resp_302 = SimpleNamespace(status=302, status_text="Found", url="https://example.com/redirect", request=req_302)
        self.page.emit("response", resp_302)

        req_404 = SimpleNamespace(resource_type="image")
        resp_404 = SimpleNamespace(status=404, status_text="Not Found", url="https://example.com/broken.png", request=req_404)
        self.page.emit("response", resp_404)

        req_500 = SimpleNamespace(resource_type="xhr")
        resp_500 = SimpleNamespace(status=500, status_text="Internal Server Error", url="https://example.com/api/data", request=req_500)
        self.page.emit("response", resp_500)

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[0].category, "http_error")
        self.assertEqual(errors[0].http_status, 404)
        self.assertEqual(errors[0].resource_type, "image")
        self.assertEqual(errors[1].category, "http_error")
        self.assertEqual(errors[1].http_status, 500)
        self.assertEqual(errors[1].resource_type, "xhr")

    def test_captures_failed_network_request(self):
        failed_req = SimpleNamespace(
            url="https://example.com/style.css",
            resource_type="stylesheet",
            failure="net::ERR_NAME_NOT_RESOLVED",
        )
        self.page.emit("requestfailed", failed_req)

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, "request_failed")
        self.assertEqual(errors[0].resource_type, "stylesheet")
        self.assertIn("ERR_NAME_NOT_RESOLVED", errors[0].message)

    def test_filters_out_blockedbyclient_safety_blocks(self):
        blocked_req = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data",
            resource_type="xhr",
            failure="net::ERR_BLOCKED_BY_CLIENT",
        )
        self.page.emit("requestfailed", blocked_req)

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 0)

    def test_filters_out_navigation_aborts(self):
        aborted_req1 = SimpleNamespace(
            url="https://example.com/page-1",
            resource_type="document",
            failure="net::ERR_ABORTED",
        )
        aborted_req2 = SimpleNamespace(
            url="https://example.com/page-2",
            resource_type="document",
            failure="NS_BINDING_ABORTED",
        )
        self.page.emit("requestfailed", aborted_req1)
        self.page.emit("requestfailed", aborted_req2)

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 0)

    def test_deduplicates_repeated_errors_within_run(self):
        req = SimpleNamespace(resource_type="xhr")
        resp = SimpleNamespace(
            status=503,
            status_text="Service Unavailable",
            url="https://example.com/api/heartbeat",
            request=req,
        )
        # Emit the exact same error 5 times (e.g. rapid polling)
        for _ in range(5):
            self.page.emit("response", resp)

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].http_status, 503)

    def test_ignores_errors_after_cancellation(self):
        self.cancel_event.set()
        self.page.emit("pageerror", Exception("Late page error after cancel"))
        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 0)

    def test_step_reference_tracks_current_step(self):
        self.current_step = 3
        self.page.emit("pageerror", Exception("Error in step 3"))
        self.current_step = 7
        self.page.emit("pageerror", Exception("Error in step 7"))

        errors = database.list_browser_errors(self.run.id, database_path=self.db_path)
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[0].step_number, 3)
        self.assertEqual(errors[1].step_number, 7)


class TestBrowserErrorsApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api_browser.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)
        self.auth_headers = {
            "Host": "127.0.0.1:8000",
            "Authorization": f"Bearer {API_TOKEN}",
            "Origin": "http://127.0.0.1:8000",
        }
        self.client = TestClient(app_module.app)
        self.run = _make_test_run(self.db_path)

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_get_browser_errors_endpoint(self):
        database.add_browser_error(
            self.run.id,
            BrowserErrorCreate(
                step_number=1,
                category="console_error",
                message="ReferenceError: $ is not defined",
                url="https://example.com/jquery-missing",
            ),
            database_path=self.db_path,
        )

        resp = self.client.get(
            f"/test-runs/{self.run.id}/browser-errors",
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["category"], "console_error")
        self.assertEqual(data[0]["message"], "ReferenceError: $ is not defined")
        self.assertEqual(data[0]["url"], "https://example.com/jquery-missing")

    def test_get_browser_errors_for_nonexistent_run_returns_404(self):
        resp = self.client.get(
            f"/test-runs/{uuid4()}/browser-errors",
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_browser_errors_requires_authentication(self):
        resp = self.client.get(
            f"/test-runs/{self.run.id}/browser-errors",
            headers={"Host": "127.0.0.1:8000"},
        )
        self.assertEqual(resp.status_code, 401)



class TestReportGeneration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_reports.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)
        self.run = _make_test_run(self.db_path)

        database.add_browser_error(
            self.run.id,
            BrowserErrorCreate(
                step_number=1,
                category="http_error",
                message="HTTP 404 Not Found",
                url="https://example.com/missing.css",
                http_status=404,
                resource_type="stylesheet",
            ),
            database_path=self.db_path,
        )

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_excel_report_includes_browser_errors_sheet(self):
        report_data = database.get_test_run_report_data(self.run.id, database_path=self.db_path)
        excel_path = generate_excel_report(report_data)
        self.assertTrue(excel_path.is_file())

        wb = load_workbook(excel_path)
        self.assertIn("Browser Errors", wb.sheetnames)
        sheet = wb["Browser Errors"]
        headers = [cell.value for cell in sheet[1]]
        self.assertIn("Category", headers)
        self.assertIn("HTTP status", headers)
        self.assertIn("URL", headers)

        row2 = [cell.value for cell in sheet[2]]
        self.assertIn("http_error", row2)
        self.assertIn(404, row2)
        self.assertIn("https://example.com/missing.css", row2)

    def test_html_report_includes_browser_findings_section(self):
        report_data = database.get_test_run_report_data(self.run.id, database_path=self.db_path)
        html_path = generate_html_report(report_data, "http://127.0.0.1:8000")
        self.assertTrue(html_path.is_file())

        html_content = html_path.read_text(encoding="utf-8")
        self.assertIn("Browser Findings", html_content)
        self.assertIn("http_error", html_content)
        self.assertIn("HTTP 404 Not Found", html_content)
        self.assertIn("https://example.com/missing.css", html_content)


class TestResultSemanticsIsolation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_semantics.db"
        self.original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        database.initialize_database(self.db_path)
        self.run = _make_test_run(self.db_path)

    def tearDown(self):
        database.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_browser_findings_do_not_mark_run_as_failed_or_bug(self):
        # Record passing step
        now = datetime.now(timezone.utc)
        database.add_test_step(
            self.run.id,
            database.TestStepCreate(
                step_number=1,
                action="inspect_page",
                status="passed",
                actual_result="Page loaded successfully",
            ),
            database_path=self.db_path,
        )
        database.update_test_run_status(self.run.id, "running", database_path=self.db_path)
        database.update_test_run_status(self.run.id, "completed", database_path=self.db_path)

        # Record browser finding (e.g. 404 resource, console error)
        database.add_browser_error(
            self.run.id,
            BrowserErrorCreate(
                step_number=1,
                category="http_error",
                message="HTTP 404 Not Found on tracking beacon",
                http_status=404,
            ),
            database_path=self.db_path,
        )
        # Log finding with browser_finding (NOT browser_error)
        database.add_execution_log(
            self.run.id,
            "browser_finding",
            "HTTP 404 Not Found on tracking beacon",
            database_path=self.db_path,
        )

        # Inspect run
        refreshed = database.get_test_run(self.run.id, database_path=self.db_path)
        self.assertEqual(refreshed.status, "completed")
        self.assertIsNone(refreshed.execution_error)
        self.assertEqual(refreshed.suspected_bug_count, 0)

        # Confirm assertion_result is not corrupted
        history_items, _ = database.list_test_run_history(database_path=self.db_path)
        history_item = next(item for item in history_items if str(item.id) == str(self.run.id))
        self.assertIsNone(history_item.execution_error)
        self.assertEqual(history_item.suspected_bug_count, 0)


if __name__ == "__main__":
    unittest.main()
