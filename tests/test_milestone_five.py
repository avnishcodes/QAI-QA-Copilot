import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright

os.environ.setdefault("GEMINI_API_KEY", "milestone-test-key")

from fastapi.testclient import TestClient

from backend import database
from backend import app as app_module
from backend.security import API_TOKEN


FRONTEND_URL = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").as_uri()


class TestFrontendReportEscaping(unittest.TestCase):
    def test_gemini_report_and_error_text_cannot_inject_html(self):
        malicious_text = '<img src=x onerror="window.__qaiXss = 1">'
        responses = [
            {"success": True, "report": malicious_text},
            {"success": False, "error": malicious_text},
        ]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script("window.__qaiXss = 0")
            current_response = {"value": responses.pop(0)}

            def respond(route):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(current_response["value"]),
                )

            page.route("http://127.0.0.1:8000/analyze-bug", respond)
            page.goto(FRONTEND_URL)

            test_responses = [current_response["value"], *responses]
            for expected_response in test_responses:
                current_response["value"] = expected_response
                page.locator("#bugInput").fill("A button does not respond.")
                page.locator("#analyzeBtn").click()
                page.wait_for_function(
                    "text => document.querySelector('#result').textContent.includes(text)",
                    arg=malicious_text,
                )

                self.assertEqual(page.locator("#result img").count(), 0)
                self.assertEqual(page.evaluate("window.__qaiXss"), 0)
                self.assertIn(malicious_text, page.locator("#result").inner_text())

            browser.close()


class TestGeminiFailureHandling(unittest.TestCase):
    def test_backend_module_can_start_without_a_gemini_key(self):
        environment = os.environ.copy()
        environment.pop("GEMINI_API_KEY", None)
        environment["PYTHON_DOTENV_DISABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from backend.app import app; print(app.title)",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "QAI - QA Copilot")

    def test_missing_key_preserves_api_and_reports_configuration_error(self):
        with TemporaryDirectory() as temporary_directory:
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "no-key.db"
            try:
                with patch.object(app_module, "client", None):
                    with TestClient(app_module.app, headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"}) as client:
                        self.assertEqual(client.get("/").status_code, 200)
                        response = client.post(
                            "/analyze-bug",
                            json={"observation": "The save button does not respond."},
                        )
                    with self.assertRaisesRegex(RuntimeError, "not configured"):
                        app_module.generate_autonomous_plan("unused")
            finally:
                database.DATABASE_PATH = original_database_path

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertIn("GEMINI_API_KEY is not configured", response.json()["error"])

    def test_provider_exception_details_are_not_returned_or_logged(self):
        provider_detail = "503 UNAVAILABLE api_key=private-marker"
        with TemporaryDirectory() as temporary_directory:
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "gemini-error.db"
            output = io.StringIO()
            try:
                with TestClient(app_module.app, headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"}) as client:
                    with (
                        patch.object(
                            app_module.client.models,
                            "generate_content",
                            side_effect=RuntimeError(provider_detail),
                        ) as generate_content,
                        patch("backend.app.time.sleep"),
                        redirect_stdout(output),
                    ):
                        response = client.post(
                            "/analyze-bug",
                            json={"observation": "The save button does not respond."},
                        )
                self.assertEqual(generate_content.call_count, 3)
            finally:
                database.DATABASE_PATH = original_database_path

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertNotIn("private-marker", response.text)
        self.assertNotIn("private-marker", output.getvalue())
