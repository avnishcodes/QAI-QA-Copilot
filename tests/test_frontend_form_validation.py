import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

FRONTEND_URL = (
    Path(__file__).resolve().parent.parent / "frontend" / "index.html"
).as_uri()

LOGIN_REQUIRED_FIELD_IDS = [
    "loginUsernameSelector",
    "loginPasswordSelector",
    "loginSubmitSelector",
    "rejectionSelector",
    "testUsername",
    "testPassword",
]

COMMON_REQUIRED_FIELD_IDS = [
    "runName",
    "runTarget",
    "runGoal",
    "runMaxSteps",
    "runTimeout",
]


class TestFrontendNewRunFormValidation(unittest.TestCase):
    """Playwright browser tests for New Test Run form validation in both modes."""

    def test_user_directed_mode_requires_login_check_settings(self):
        """In Configured invalid-login test mode, login check fields are required and validated."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(FRONTEND_URL)
                page.click('[data-page="new-run"]')

                # Verify mode is user-directed initially
                self.assertEqual(page.locator("#runMode").input_value(), "user-directed")

                # Verify login fields are required and enabled
                for fid in LOGIN_REQUIRED_FIELD_IDS:
                    has_attr = page.locator(f"#{fid}").evaluate("el => el.hasAttribute('required')")
                    prop = page.evaluate(f"document.getElementById('{fid}').required")
                    disabled = page.evaluate(f"document.getElementById('{fid}').disabled")
                    self.assertTrue(has_attr, f"{fid} should have required attribute in user-directed mode")
                    self.assertTrue(prop, f"{fid} required property should be True in user-directed mode")
                    self.assertFalse(disabled, f"{fid} should be enabled in user-directed mode")

                # allowFormSubmission should be disabled in user-directed mode
                self.assertTrue(page.evaluate("document.getElementById('allowFormSubmission').disabled"))

                # Form must fail validation when login selectors are empty
                page.fill("#runTarget", "https://example.com/login")
                is_valid = page.evaluate("document.getElementById('websiteRunForm').checkValidity()")
                self.assertFalse(is_valid, "Form should fail validation when required login fields are empty")
            finally:
                browser.close()

    def test_autonomous_mode_does_not_block_on_login_check_settings(self):
        """In Bounded autonomous exploration mode, login fields are not required and do not block submission."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(FRONTEND_URL)
                page.click('[data-page="new-run"]')

                # Switch to autonomous-exploration
                page.select_option("#runMode", "autonomous-exploration")
                self.assertEqual(page.locator("#runMode").input_value(), "autonomous-exploration")

                # Verify login fields are not required and disabled
                for fid in LOGIN_REQUIRED_FIELD_IDS:
                    has_attr = page.locator(f"#{fid}").evaluate("el => el.hasAttribute('required')")
                    prop = page.evaluate(f"document.getElementById('{fid}').required")
                    disabled = page.evaluate(f"document.getElementById('{fid}').disabled")
                    self.assertFalse(has_attr, f"{fid} must not have required attribute in autonomous mode")
                    self.assertFalse(prop, f"{fid} required property must be False in autonomous mode")
                    self.assertTrue(disabled, f"{fid} must be disabled in autonomous mode")

                # allowFormSubmission should be enabled in autonomous mode
                self.assertFalse(page.evaluate("document.getElementById('allowFormSubmission').disabled"))

                # Provide valid target URL
                page.fill("#runTarget", "https://example.com/portal")

                # Form must pass constraint validation
                is_valid = page.evaluate("document.getElementById('websiteRunForm').checkValidity()")
                self.assertTrue(is_valid, "Form should be valid in autonomous exploration mode")

                # Verify form submits without triggering browser validation errors
                submitted = [False]
                page.expose_function("onTestSubmit", lambda: submitted.__setitem__(0, True))
                page.evaluate("""() => {
                    document.getElementById('websiteRunForm').addEventListener('submit', (e) => {
                        e.preventDefault();
                        window.onTestSubmit();
                    }, { capture: true });
                }""")

                page.click("#startRunBtn")
                page.wait_for_timeout(100)
                self.assertTrue(submitted[0], "Submit event must fire without browser validation blocking")
            finally:
                browser.close()

    def test_mode_switching_dynamically_toggles_required_state(self):
        """Switching between modes correctly updates required and disabled states repeatedly."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(FRONTEND_URL)
                page.click('[data-page="new-run"]')

                # Switch to autonomous
                page.select_option("#runMode", "autonomous-exploration")
                for fid in LOGIN_REQUIRED_FIELD_IDS:
                    self.assertFalse(page.evaluate(f"document.getElementById('{fid}').required"))
                    self.assertTrue(page.evaluate(f"document.getElementById('{fid}').disabled"))

                # Switch back to user-directed
                page.select_option("#runMode", "user-directed")
                for fid in LOGIN_REQUIRED_FIELD_IDS:
                    self.assertTrue(page.evaluate(f"document.getElementById('{fid}').required"))
                    self.assertFalse(page.evaluate(f"document.getElementById('{fid}').disabled"))

                # Switch again to autonomous
                page.select_option("#runMode", "autonomous-exploration")
                for fid in LOGIN_REQUIRED_FIELD_IDS:
                    self.assertFalse(page.evaluate(f"document.getElementById('{fid}').required"))
                    self.assertTrue(page.evaluate(f"document.getElementById('{fid}').disabled"))
            finally:
                browser.close()

    def test_common_required_fields_remain_required_in_both_modes(self):
        """Common fields (name, target URL, goal, max steps, timeout) remain required in both modes."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(FRONTEND_URL)
                page.click('[data-page="new-run"]')

                for mode in ["user-directed", "autonomous-exploration"]:
                    page.select_option("#runMode", mode)
                    for fid in COMMON_REQUIRED_FIELD_IDS:
                        prop = page.evaluate(f"document.getElementById('{fid}').required")
                        disabled = page.evaluate(f"document.getElementById('{fid}').disabled")
                        self.assertTrue(prop, f"{fid} should be required in mode {mode}")
                        self.assertFalse(disabled, f"{fid} should be enabled in mode {mode}")
            finally:
                browser.close()

    def test_synthetic_username_supports_numeric_phone_and_standard_identifiers(self):
        """Synthetic test username field accepts 10-digit phone numbers and standard identifiers."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(FRONTEND_URL)
                page.click('[data-page="new-run"]')

                # Verify placeholder is present and mentions numeric identifiers
                placeholder = page.locator("#testUsername").get_attribute("placeholder")
                self.assertIsNotNone(placeholder)
                self.assertIn("9876543210", placeholder)

                # Test 10-digit mobile number
                page.fill("#testUsername", "9876543210")
                self.assertEqual(page.locator("#testUsername").input_value(), "9876543210")
                self.assertTrue(page.evaluate("document.getElementById('testUsername').checkValidity()"))

                # Test E.164 formatted phone number
                page.fill("#testUsername", "+919876543210")
                self.assertEqual(page.locator("#testUsername").input_value(), "+919876543210")
                self.assertTrue(page.evaluate("document.getElementById('testUsername').checkValidity()"))

                # Test standard alphanumeric username
                page.fill("#testUsername", "synthetic_user_42")
                self.assertTrue(page.evaluate("document.getElementById('testUsername').checkValidity()"))

                # Test standard email username
                page.fill("#testUsername", "qai-invalid-user@example.invalid")
                self.assertTrue(page.evaluate("document.getElementById('testUsername').checkValidity()"))

                # Test empty username fails validation when required
                page.fill("#testUsername", "")
                self.assertFalse(page.evaluate("document.getElementById('testUsername').checkValidity()"))

                # Test form submission with 10-digit phone number
                page.fill("#runTarget", "https://example.com/login")
                page.fill("#loginUsernameSelector", "input[aria-label='Phone number']")
                page.fill("#loginPasswordSelector", "input[type='password']")
                page.fill("#loginSubmitSelector", "button[type='submit']")
                page.fill("#rejectionSelector", ".error-banner")
                page.fill("#testUsername", "9876543210")
                page.fill("#testPassword", "invalid-pass-123")

                submitted = [False]
                page.expose_function("onPhoneLoginSubmit", lambda: submitted.__setitem__(0, True))
                page.evaluate("""() => {
                    document.getElementById('websiteRunForm').addEventListener('submit', (e) => {
                        e.preventDefault();
                        window.onPhoneLoginSubmit();
                    }, { capture: true });
                }""")
                page.click("#startRunBtn")
                page.wait_for_timeout(100)
                self.assertTrue(submitted[0], "Form with numeric phone username must submit without validation errors")
            finally:
                browser.close()


class TestSyntheticUsernameSchemaValidation(unittest.TestCase):
    """Unit tests for StartTestRunRequest synthetic username validation."""

    def test_numeric_phone_number_string(self):
        from backend.schemas import StartTestRunRequest

        req = StartTestRunRequest(username="9876543210")
        self.assertEqual(req.username, "9876543210")

    def test_e164_phone_number_string(self):
        from backend.schemas import StartTestRunRequest

        req = StartTestRunRequest(username="+919876543210")
        self.assertEqual(req.username, "+919876543210")

    def test_integer_numeric_identifier_coerced_to_string(self):
        from backend.schemas import StartTestRunRequest

        req = StartTestRunRequest.model_validate({"username": 9876543210})
        self.assertEqual(req.username, "9876543210")

    def test_standard_synthetic_email_and_username(self):
        from backend.schemas import StartTestRunRequest

        req1 = StartTestRunRequest(username="qai-invalid-user@example.invalid")
        self.assertEqual(req1.username, "qai-invalid-user@example.invalid")

        req2 = StartTestRunRequest(username="custom-test-user")
        self.assertEqual(req2.username, "custom-test-user")

    def test_whitespace_padded_username_is_trimmed(self):
        from backend.schemas import StartTestRunRequest

        req = StartTestRunRequest(username="  9876543210  ")
        self.assertEqual(req.username, "9876543210")

    def test_blank_and_whitespace_only_username_rejected(self):
        from pydantic import ValidationError
        from backend.schemas import StartTestRunRequest

        with self.assertRaises(ValidationError):
            StartTestRunRequest(username="")

        with self.assertRaises(ValidationError):
            StartTestRunRequest(username="    ")

    def test_boolean_and_invalid_types_rejected(self):
        from pydantic import ValidationError
        from backend.schemas import StartTestRunRequest

        with self.assertRaises(ValidationError):
            StartTestRunRequest.model_validate({"username": True})

        with self.assertRaises(ValidationError):
            StartTestRunRequest.model_validate({"username": False})

        with self.assertRaises(ValidationError):
            StartTestRunRequest.model_validate({"username": ["9876543210"]})


if __name__ == "__main__":
    unittest.main()
