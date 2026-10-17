import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import app
from backend.security import API_TOKEN


class TestApiSecurity(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(
            app,
            headers={"Host": "127.0.0.1:8000"},
        )

    def tearDown(self):
        self.client.close()

    def test_sensitive_routes_require_authorization(self):
        run_id = str(uuid4())
        requests = (
            ("get", "/test-runs"),
            ("post", "/test-runs"),
            ("get", "/test-runs/not-a-run"),
            ("get", "/test-runs/not-a-run/steps"),
            ("post", f"/test-runs/{run_id}/start"),
            ("post", f"/test-runs/{run_id}/cancel"),
            ("get", f"/test-runs/{run_id}/reports/html"),
            ("get", f"/test-runs/{run_id}/reports/excel"),
            ("get", f"/test-runs/{run_id}/steps/1/screenshot"),
            ("post", "/analyze-bug"),
        )
        for method, path in requests:
            if method == "post":
                payload = (
                    {
                        "run_name": "unauthorized",
                        "platform": "website",
                        "mode": "user-directed",
                        "target": "https://example.test",
                        "goal": "unauthorized",
                    }
                    if path == "/test-runs"
                    else {"observation": "unauthorized"}
                )
                if path != "/analyze-bug" and path != "/test-runs":
                    payload = {}
                response = self.client.post(path, json=payload)
            else:
                response = self.client.get(path)
            self.assertEqual(response.status_code, 401, (method, path, response.text))

    def test_bearer_token_and_invalid_token(self):
        response = self.client.get(
            "/test-runs",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
        self.assertEqual(response.status_code, 200)
        invalid = self.client.get(
            "/test-runs",
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(invalid.status_code, 401)

    def test_session_bootstrap_requires_exact_origin_and_host(self):
        missing_origin = self.client.post("/auth/session")
        self.assertEqual(missing_origin.status_code, 403)
        untrusted = self.client.post(
            "/auth/session",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(untrusted.status_code, 403)
        valid = self.client.post(
            "/auth/session",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        self.assertEqual(valid.status_code, 200)
        self.assertIn("qai_session=", valid.headers["set-cookie"])
        self.assertNotIn(API_TOKEN, valid.headers["set-cookie"])

    def test_session_cookie_authorizes_same_origin_requests(self):
        bootstrap = self.client.post(
            "/auth/session",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        self.assertEqual(bootstrap.status_code, 200)
        response = self.client.get(
            "/test-runs",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/test-runs", headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )

    def test_cors_is_explicit_and_analyze_bug_contract_is_preserved(self):
        preflight = self.client.options(
            "/test-runs",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotEqual(preflight.headers.get("access-control-allow-origin"), "https://evil.example")
        allowed = self.client.options(
            "/test-runs",
            headers={
                "Origin": "http://127.0.0.1:8000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://127.0.0.1:8000")
        response = self.client.post(
            "/analyze-bug",
            json={"observation": ""},
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
        self.assertEqual(set(response.json()), {"success", "error"})
        self.assertFalse(response.json()["success"])


if __name__ == "__main__":
    unittest.main()
