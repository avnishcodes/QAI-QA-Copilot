import os
import ipaddress
import socket
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "milestone-test-key")

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import database
from backend import website_runner
from backend.app import app
from backend.security import API_TOKEN
from backend.schemas import TestRunCreate, WebsiteAction
from backend.website_runner import (
    TargetValidationError,
    build_login_action_plan,
    execute_website_run,
    validate_action_plan,
    validate_target_syntax,
    validate_target_url,
)


def make_run(database_path: Path, *, rejection_text: str | None = "Invalid credentials"):
    database.initialize_database(database_path)
    return database.create_test_run(
        TestRunCreate(
            run_name="Invalid login check",
            platform="website",
            mode="user-directed",
            target="https://example.com/login",
            goal="Submit invalid credentials and verify rejection.",
            expected_results=["The rejection message is visible."],
            max_steps=10,
            login_username_selector="#username",
            login_password_selector="#password",
            login_submit_selector="button[type='submit']",
            rejection_selector=".error-message",
            rejection_text=rejection_text,
            authenticated_selector=".account-menu",
        ),
        database_path,
    )


class FakeLocator:
    def __init__(self, selector: str):
        self.selector = selector
        self.values: list[str] = []

    def count(self):
        return 4

    def fill(self, value: str):
        self.values.append(value)

    def click(self):
        return None

    def is_visible(self):
        return self.selector == ".error-message"

    def inner_text(self):
        return "Invalid credentials"


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.default_timeout = 0
        self.fill_values: list[str] = []
        self.locators: dict[str, FakeLocator] = {}

    def goto(self, url: str, wait_until: str):
        if wait_until != "domcontentloaded":
            raise AssertionError("Unexpected page load mode.")
        self.url = url

    def title(self):
        return "Login"

    def locator(self, selector: str):
        if selector == "button, a, input, select, textarea":
            return FakeLocator(selector)
        return self.locators.setdefault(selector, FakeLocator(selector))

    def set_default_timeout(self, timeout: int):
        self.default_timeout = timeout

    def screenshot(self, path: str, full_page: bool):
        if not full_page:
            raise AssertionError("Expected a full-page screenshot.")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"test screenshot")


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.route_handler = None

    def route(self, pattern: str, handler):
        if pattern != "**/*":
            raise AssertionError("Unexpected browser route scope.")
        self.route_handler = handler

    def new_page(self):
        return self.page

    def close(self):
        return None


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()

    def new_context(self, service_workers: str):
        if service_workers != "block":
            raise AssertionError("Expected service workers to be blocked.")
        return self.context

    def close(self):
        return None


class FakePlaywrightManager:
    def __init__(self):
        self.browser = FakeBrowser()

        def launch(**kwargs):
            if not kwargs["headless"] or kwargs["timeout"] <= 0:
                raise AssertionError("Unexpected Chromium launch options.")
            if not kwargs["args"][0].startswith("--host-resolver-rules=MAP "):
                raise AssertionError("Target DNS was not pinned for Chromium.")
            return self.browser

        self.chromium = SimpleNamespace(launch=launch)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None or exc_value is not None or traceback is not None:
            raise AssertionError("Unexpected fake browser context exception.")
        return False


class TestWebsiteActionPlans(unittest.TestCase):
    def test_login_plan_contains_only_configured_permitted_actions(self):
        with TemporaryDirectory() as temporary_directory:
            run = make_run(Path(temporary_directory) / "actions.db")
            actions = build_login_action_plan(run)

        self.assertEqual(
            [action.action for action in actions],
            [
                "inspect_page",
                "fill_field",
                "fill_field",
                "click_control",
                "check_visibility",
                "verify_authentication",
                "capture_screenshot",
            ],
        )

    def test_action_plan_rejects_unapproved_or_invalid_actions(self):
        with self.assertRaises(ValidationError):
            validate_action_plan(
                [{"action": "run_shell_command", "command": "whoami"}],
                10,
            )

        with self.assertRaises(ValidationError):
            WebsiteAction(action="fill_field", selector="#password")

        with self.assertRaises(ValueError):
            validate_action_plan(
                [{"action": "inspect_page"}] * 3,
                2,
            )

        with self.assertRaises(ValidationError):
            WebsiteAction(action="click_control", selector="xpath=//button")


class TestWebsiteTargetValidation(unittest.TestCase):
    def test_private_metadata_and_non_http_targets_are_rejected(self):
        for target in (
            "http://127.0.0.1/",
            "http://10.0.0.4/",
            "http://169.254.169.254/latest/meta-data/",
            "http://168.63.129.16/",
            "http://localhost/",
            "file:///C:/Windows/win.ini",
        ):
            with self.subTest(target=target):
                with self.assertRaises(TargetValidationError):
                    validate_target_syntax(target)

    def test_hostname_resolving_to_private_address_is_rejected(self):
        with patch.dict(os.environ, {"QAI_ALLOWED_HOSTS": ""}):
            with patch(
                "backend.website_runner.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 443))
                ],
            ):
                with self.assertRaises(TargetValidationError):
                    validate_target_url("https://public.example")

    def test_old_database_schema_is_upgraded_in_place(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "legacy.db"
            connection = database.sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE test_runs (
                    id TEXT PRIMARY KEY, run_name TEXT NOT NULL,
                    platform TEXT NOT NULL, mode TEXT NOT NULL,
                    target TEXT NOT NULL, goal TEXT NOT NULL,
                    expected_results TEXT NOT NULL, max_steps INTEGER NOT NULL,
                    max_duration_seconds INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE test_steps (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    step_number INTEGER NOT NULL, action TEXT NOT NULL,
                    page_or_screen TEXT, expected_result TEXT,
                    actual_result TEXT, status TEXT NOT NULL,
                    duration_ms INTEGER, error_details TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.close()

            database.initialize_database(database_path)

            with database._connection(database_path) as upgraded:
                run_columns = {
                    row["name"]
                    for row in upgraded.execute("PRAGMA table_info(test_runs)")
                }
                step_columns = {
                    row["name"]
                    for row in upgraded.execute("PRAGMA table_info(test_steps)")
                }
            self.assertIn("started_at", run_columns)
            self.assertIn("login_username_selector", run_columns)
            self.assertIn("screenshot_path", step_columns)

    def test_configured_allowlist_rejects_unlisted_host(self):
        with patch.dict(os.environ, {"QAI_ALLOWED_HOSTS": "qa.example.com"}):
            with self.assertRaises(TargetValidationError):
                validate_target_syntax("https://other.example.com/login")


class TestWebsiteRunner(unittest.TestCase):
    def run_fake_browser(self, rejection_text: str | None):
        with TemporaryDirectory() as temporary_directory:
            original_evidence_dir = website_runner.EVIDENCE_DIR
            website_runner.EVIDENCE_DIR = Path(temporary_directory) / "evidence"
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "runner.db"
            run = make_run(
                database.DATABASE_PATH,
                rejection_text=rejection_text,
            )
            database.update_test_run_status(run.id, "running")
            self.assertTrue(website_runner.try_reserve_run())

            def validate_fake_url(url: str, original: str | None) -> str:
                self.assertTrue(url.startswith("https://example.com/"))
                self.assertIn(original, {None, "example.com"})
                return "example.com"

            with patch(
                "backend.website_runner.sync_playwright",
                return_value=FakePlaywrightManager(),
            ), patch(
                "backend.website_runner._resolve_public_addresses",
                return_value={ipaddress.ip_address("93.184.216.34")},
            ):
                execute_website_run(
                    run.id,
                    website_runner.StartTestRunRequest(
                        username="invalid-user",
                        password="never-persist-this-password",
                    ),
                    threading.Event(),
                    target_validator=validate_fake_url,
                )
            updated_run = database.get_test_run(run.id)
            steps = database.list_test_steps(run.id)
            evidence_files = list(
                (Path(temporary_directory) / "evidence").rglob("*.png")
            )
            website_runner.EVIDENCE_DIR = original_evidence_dir
            database.DATABASE_PATH = original_database_path
            return updated_run, steps, evidence_files

    def test_successful_assertion_records_steps_and_screenshot(self):
        run, steps, evidence_files = self.run_fake_browser("Invalid credentials")

        self.assertEqual(run.status, "completed")
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.ended_at)
        self.assertEqual(len(steps), 7)
        self.assertTrue(all(step.status == "passed" for step in steps))
        self.assertTrue(steps[-1].screenshot_path.endswith(".png"))
        self.assertEqual(len(evidence_files), 1)
        self.assertNotIn("never_persist_this_password", repr(steps))

    def test_failed_assertion_is_recorded_without_marking_execution_fake_or_passed(self):
        run, steps, evidence_files = self.run_fake_browser("Access denied")

        self.assertEqual(run.status, "completed")
        self.assertEqual(steps[4].status, "failed")
        self.assertIn("did not match", steps[4].error_details)
        self.assertEqual(steps[-1].status, "passed")
        self.assertTrue(evidence_files)


class TestWebsiteRunApi(unittest.TestCase):
    def test_create_start_status_steps_and_cancel_route_contract(self):
        with TemporaryDirectory() as temporary_directory:
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "api.db"
            try:
                with TestClient(app, headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"}) as client:
                    payload = {
                        "run_name": "API login test",
                        "platform": "website",
                        "mode": "user-directed",
                        "target": "https://example.com/login",
                        "goal": "Verify invalid login is rejected.",
                        "max_steps": 10,
                        "login_username_selector": "#username",
                        "login_password_selector": "#password",
                        "login_submit_selector": "button[type='submit']",
                        "rejection_selector": ".error-message",
                    }
                    created = client.post("/test-runs", json=payload)
                    self.assertEqual(created.status_code, 201)
                    run_id = created.json()["id"]

                    def complete_without_browser(run_id, credentials, cancel_event):
                        self.assertTrue(credentials.password)
                        self.assertFalse(cancel_event.is_set())
                        database.update_test_run_status(run_id, "completed")
                        with website_runner._ACTIVE_RUNS_LOCK:
                            website_runner._ACTIVE_RUNS.pop(run_id, None)
                        website_runner.release_run_slot()

                    with (
                        patch("backend.app.validate_target_url", return_value="example.com"),
                        patch(
                            "backend.app.execute_website_run",
                            side_effect=complete_without_browser,
                        ),
                    ):
                        started = client.post(
                            f"/test-runs/{run_id}/start",
                            json={
                                "username": "invalid",
                                "password": "do-not-store-this",
                            },
                        )
                    self.assertEqual(started.status_code, 202)
                    self.assertEqual(started.json()["status"], "running")
                    self.assertNotIn("password", started.json())

                    status = client.get(f"/test-runs/{run_id}")
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json()["status"], "completed")
                    self.assertEqual(
                        client.get(f"/test-runs/{run_id}/steps").status_code,
                        200,
                    )

                    self.assertEqual(
                        client.post(f"/test-runs/{run_id}/cancel").status_code,
                        409,
                    )
            finally:
                database.DATABASE_PATH = original_database_path

    def test_cancel_requests_signal_an_active_run(self):
        with TemporaryDirectory() as temporary_directory:
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "cancel.db"
            try:
                with TestClient(app, headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"}) as client:
                    created = client.post(
                        "/test-runs",
                        json={
                            "run_name": "Cancelable login test",
                            "platform": "website",
                            "mode": "user-directed",
                            "target": "https://example.com/login",
                            "goal": "Check rejection.",
                            "max_steps": 10,
                            "login_username_selector": "#username",
                            "login_password_selector": "#password",
                            "login_submit_selector": "button[type='submit']",
                            "rejection_selector": ".error",
                        },
                    )
                    run_id = created.json()["id"]
                    with (
                        patch(
                            "backend.app.validate_target_url",
                            return_value="example.com",
                        ),
                        patch("backend.app.execute_website_run"),
                    ):
                        started = client.post(f"/test-runs/{run_id}/start")
                        self.assertEqual(started.status_code, 202)
                        cancelled = client.post(f"/test-runs/{run_id}/cancel")

                    self.assertEqual(cancelled.status_code, 200)
                    with website_runner._ACTIVE_RUNS_LOCK:
                        event = website_runner._ACTIVE_RUNS.pop(run_id)
                    self.assertTrue(event.is_set())
                    database.update_test_run_status(run_id, "cancelled")
                    website_runner.release_run_slot()
            finally:
                database.DATABASE_PATH = original_database_path

    def test_create_rejects_unsafe_target(self):
        with TemporaryDirectory() as temporary_directory:
            original_database_path = database.DATABASE_PATH
            database.DATABASE_PATH = Path(temporary_directory) / "invalid.db"
            try:
                with TestClient(app, headers={"Host": "127.0.0.1:8000", "Authorization": f"Bearer {API_TOKEN}"}) as client:
                    response = client.post(
                        "/test-runs",
                        json={
                            "run_name": "Unsafe target",
                            "platform": "website",
                            "mode": "user-directed",
                            "target": "http://127.0.0.1/admin",
                            "goal": "Test.",
                        },
                    )
                    self.assertEqual(response.status_code, 422)
            finally:
                database.DATABASE_PATH = original_database_path


class TestChromiumInstallation(unittest.TestCase):
    def test_real_chromium_opens_a_page(self):
        with website_runner.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("<title>QAI browser smoke test</title><p>Ready</p>")
                self.assertEqual(page.title(), "QAI browser smoke test")
                self.assertEqual(page.locator("p").inner_text(), "Ready")
            finally:
                browser.close()

    def test_real_chromium_executes_login_flow_against_intercepted_fixture(self):
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            original_database_path = database.DATABASE_PATH
            original_evidence_dir = website_runner.EVIDENCE_DIR
            database.DATABASE_PATH = temporary_root / "real-browser.db"
            website_runner.EVIDENCE_DIR = temporary_root / "evidence"
            try:
                run = make_run(database.DATABASE_PATH)
                database.update_test_run_status(run.id, "running")
                self.assertTrue(website_runner.try_reserve_run())
                fixture_html = """
                    <!doctype html>
                    <html><head><title>Login fixture</title></head>
                    <body>
                      <form id="login">
                        <input id="username" name="username">
                        <input id="password" name="password" type="password">
                        <button type="submit">Sign in</button>
                      </form>
                      <p class="error-message" hidden></p>
                      <div class="account-menu" hidden>Signed in</div>
                      <script>
                        document.querySelector("#login").addEventListener("submit", event => {
                          event.preventDefault();
                          const error = document.querySelector(".error-message");
                          error.textContent = "Invalid credentials";
                          error.hidden = false;
                        });
                      </script>
                    </body></html>
                """

                def fulfill_fixture(route):
                    if route.request.url == run.target:
                        route.fulfill(
                            status=200,
                            content_type="text/html",
                            body=fixture_html,
                        )
                    else:
                        route.abort()
                    return True

                with (
                    patch.dict(os.environ, {"QAI_ALLOWED_HOSTS": ""}),
                    patch(
                        "backend.website_runner.socket.getaddrinfo",
                        return_value=[
                            (
                                socket.AF_INET,
                                socket.SOCK_STREAM,
                                6,
                                "",
                                ("93.184.216.34", 443),
                            )
                        ],
                    ),
                ):
                    execute_website_run(
                        run.id,
                        website_runner.StartTestRunRequest(
                            username="invalid@example.invalid",
                            password="synthetic-password",
                        ),
                        threading.Event(),
                        request_interceptor=fulfill_fixture,
                    )

                updated_run = database.get_test_run(run.id)
                steps = database.list_test_steps(run.id)
                self.assertEqual(updated_run.status, "completed")
                self.assertEqual(len(steps), 7)
                self.assertTrue(all(step.status == "passed" for step in steps))
                self.assertTrue(
                    (
                        temporary_root
                        / "evidence"
                        / run.id
                        / "step-7.png"
                    ).exists()
                )
            finally:
                database.DATABASE_PATH = original_database_path
                website_runner.EVIDENCE_DIR = original_evidence_dir


if __name__ == "__main__":
    unittest.main()
