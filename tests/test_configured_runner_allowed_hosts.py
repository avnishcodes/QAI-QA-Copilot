import ipaddress
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from backend import database, website_runner
from backend.schemas import StartTestRunRequest, TestRunCreate, WebsiteAction
from backend.website_runner import (
    TargetValidationError,
    _run_action,
    execute_website_run,
    validate_run_target_url,
)


class TestConfiguredRunnerAllowedHosts(unittest.TestCase):
    """Regression tests for configured runner allowed_hosts and inspect_page SPA timing."""

    def test_hostname_authorized_for_run_is_allowed(self):
        """Explicit allowed_hosts in a test run are approved by validate_run_target_url."""
        target_host = "meatzaar-app.pages.dev"
        allowed_hosts = ["meat-delivery-backend.jayantachungkrang3.workers.dev"]

        # Target host itself is allowed
        self.assertEqual(
            validate_run_target_url(
                "https://meatzaar-app.pages.dev/login",
                target_host,
                allowed_hosts,
                resolve_host=False,
            ),
            target_host,
        )

        # Explicitly authorized API host is allowed
        self.assertEqual(
            validate_run_target_url(
                "https://meat-delivery-backend.jayantachungkrang3.workers.dev/api/auth/login",
                target_host,
                allowed_hosts,
                resolve_host=False,
            ),
            "meat-delivery-backend.jayantachungkrang3.workers.dev",
        )

    def test_unauthorized_hostname_remains_blocked(self):
        """Hostnames not in target_host or allowed_hosts are rejected with TargetValidationError."""
        target_host = "meatzaar-app.pages.dev"
        allowed_hosts = ["meat-delivery-backend.jayantachungkrang3.workers.dev"]

        # An unrelated workers.dev domain must remain blocked (no wildcard bypass)
        with self.assertRaises(TargetValidationError):
            validate_run_target_url(
                "https://evil-backend.workers.dev/api/auth/login",
                target_host,
                allowed_hosts,
                resolve_host=False,
            )

        # Another arbitrary domain must remain blocked
        with self.assertRaises(TargetValidationError):
            validate_run_target_url(
                "https://malicious-site.example/steal",
                target_host,
                allowed_hosts,
                resolve_host=False,
            )

    def test_ssrf_and_private_ip_restrictions_remain_enforced(self):
        """Private/loopback IPs, metadata services, and bad schemes are blocked."""
        target_host = "meatzaar-app.pages.dev"
        allowed_hosts = ["meat-delivery-backend.jayantachungkrang3.workers.dev"]

        dangerous_urls = [
            "http://127.0.0.1/status",
            "http://localhost/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "file:///etc/passwd",
            "ftp://files.example.com/",
            "https://admin:secret@meatzaar-app.pages.dev/login",
        ]

        for url in dangerous_urls:
            with self.subTest(url=url):
                with self.assertRaises(TargetValidationError):
                    validate_run_target_url(
                        url,
                        target_host,
                        allowed_hosts,
                        resolve_host=False,
                    )

    def test_guard_request_aborts_unauthorized_and_continues_authorized(self):
        """In execute_website_run, guard_request aborts unauthorized and continues authorized requests."""
        target_host = "meatzaar-app.pages.dev"
        run_allowed_hosts = ["meat-delivery-backend.jayantachungkrang3.workers.dev"]

        def make_mock_route(url: str):
            route = MagicMock()
            route.request.url = url
            return route

        def guard_request(route):
            request_url = route.request.url
            if request_url.startswith(("data:", "blob:")):
                route.continue_()
                return
            try:
                validate_run_target_url(
                    request_url,
                    target_host,
                    run_allowed_hosts,
                    resolve_host=False,
                )
            except TargetValidationError:
                route.abort("blockedbyclient")
                return
            route.continue_()

        # Authorized target request -> continued
        target_route = make_mock_route("https://meatzaar-app.pages.dev/assets/index.js")
        guard_request(target_route)
        target_route.continue_.assert_called_once()
        target_route.abort.assert_not_called()

        # Authorized backend request -> continued
        api_route = make_mock_route(
            "https://meat-delivery-backend.jayantachungkrang3.workers.dev/api/auth/login"
        )
        guard_request(api_route)
        api_route.continue_.assert_called_once()
        api_route.abort.assert_not_called()

        # Unauthorized request -> aborted
        unauthorized_route = make_mock_route("https://evil.example.com/exfiltrate")
        guard_request(unauthorized_route)
        unauthorized_route.abort.assert_called_once_with("blockedbyclient")

        # SSRF request -> aborted
        ssrf_route = make_mock_route("http://169.254.169.254/latest/meta-data")
        guard_request(ssrf_route)
        ssrf_route.abort.assert_called_once_with("blockedbyclient")

    def test_inspect_page_waits_for_spa_elements_and_counts_react_native_controls(self):
        """inspect_page waits for client-rendered SPA elements and counts div[tabindex='0']."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                # An SPA fixture that mounts controls asynchronously after domcontentloaded
                spa_html = """<!DOCTYPE html>
<html>
<head><title>SPA App</title></head>
<body>
    <div id="root"></div>
    <script>
        setTimeout(() => {
            const root = document.getElementById('root');
            root.innerHTML = `
                <input type="tel" aria-label="Phone number">
                <input type="password" aria-label="Password">
                <div tabindex="0">SIGN IN</div>
                <div role="button">Help</div>
                <a href="#register">Create Account</a>
            `;
            document.title = "Hydrated SPA App";
        }, 150);
    </script>
</body>
</html>"""
                with TemporaryDirectory() as temp_dir:
                    spa_file = Path(temp_dir) / "spa_login.html"
                    spa_file.write_text(spa_html, encoding="utf-8")
                    action = WebsiteAction(action="inspect_page")

                    success, message, err, details = _run_action(
                        page=page,
                        action=action,
                        credentials=StartTestRunRequest(),
                        target_url=spa_file.as_uri(),
                        screenshot_file=None,
                    )

                    self.assertTrue(success)
                    self.assertIn("found 5 interactive controls", message)
                    self.assertIn("Hydrated SPA App", message)
                    self.assertIsNone(err)
            finally:
                browser.close()

    def test_inspect_page_counts_static_controls_without_delay(self):
        """inspect_page counts static HTML controls immediately."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                static_html = """<!DOCTYPE html>
<html>
<head><title>Static Login</title></head>
<body>
    <form>
        <input type="text" id="username">
        <input type="password" id="password">
        <button type="submit">Log in</button>
    </form>
</body>
</html>"""
                with TemporaryDirectory() as temp_dir:
                    static_file = Path(temp_dir) / "static_login.html"
                    static_file.write_text(static_html, encoding="utf-8")
                    action = WebsiteAction(action="inspect_page")

                    success, message, err, details = _run_action(
                        page=page,
                        action=action,
                        credentials=StartTestRunRequest(),
                        target_url=static_file.as_uri(),
                        screenshot_file=None,
                    )

                    self.assertTrue(success)
                    self.assertIn("found 3 interactive controls", message)
                    self.assertIn("Static Login", message)
            finally:
                browser.close()

    def test_execute_website_run_allows_configured_allowed_hosts(self):
        """execute_website_run configures resolver rules and allows requests to run.allowed_hosts."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "qai_test.db"
            database.initialize_database(db_path)
            original_db = database.DATABASE_PATH
            database.DATABASE_PATH = db_path
            try:
                run = database.create_test_run(
                    TestRunCreate(
                        run_name="Allowed Hosts Test",
                        platform="website",
                        mode="user-directed",
                        target="https://example.com/login",
                        goal="Test allowed hosts in execute_website_run",
                        expected_results=["The rejection message is visible."],
                        allowed_hosts=["api.example.com"],
                        max_steps=10,
                        login_username_selector="#username",
                        login_password_selector="#password",
                        login_submit_selector="button[type='submit']",
                        rejection_selector=".error-message",
                        rejection_text="Invalid credentials",
                    ),
                    db_path,
                )

                fixture_html = """<!DOCTYPE html>
<html>
<body>
    <input id="username">
    <input id="password">
    <button type="submit">Log in</button>
    <div class="error-message">Invalid credentials</div>
    <script>
        fetch("https://api.example.com/v1/ping").catch(() => {});
    </script>
</body>
</html>"""

                recorded_routes = []

                def mock_interceptor(route):
                    url = route.request.url
                    recorded_routes.append(url)
                    if "api.example.com" in url:
                        route.fulfill(status=200, body="pong")
                        return True
                    route.fulfill(status=200, content_type="text/html", body=fixture_html)
                    return True

                database.update_test_run_status(run.id, "running", db_path)
                self.assertTrue(website_runner.try_reserve_run())

                with patch(
                    "backend.website_runner._resolve_public_addresses",
                    return_value={ipaddress.ip_address("93.184.216.34")},
                ):
                    execute_website_run(
                        run.id,
                        StartTestRunRequest(),
                        threading.Event(),
                        request_interceptor=mock_interceptor,
                    )

                updated_run = database.get_test_run(run.id, db_path)
                self.assertIsNotNone(updated_run)
                self.assertEqual(updated_run.status, "completed")
                self.assertEqual(updated_run.assertion_result, "passed")
                self.assertTrue(any("api.example.com" in r for r in recorded_routes))
            finally:
                database.DATABASE_PATH = original_db


if __name__ == "__main__":
    unittest.main()
