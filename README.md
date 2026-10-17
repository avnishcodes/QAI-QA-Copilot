# QAI - QA Copilot

QAI is a local QA defect analysis assistant. The application includes a
Gemini-powered bug-report endpoint, SQLite-backed test runs, configured
Playwright login checks, bounded autonomous website exploration, and
execution-history Excel/HTML reports. Android automation is not implemented.

## Requirements

- Python 3.10 or newer
- An optional Gemini API key for Gemini-backed bug analysis and autonomous
  planning (website testing remains available without it, using deterministic
  autonomous actions)
- Playwright Chromium

## Setup (Windows PowerShell)

From the project directory:

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
```

Create `backend\.env` using `.env.example` as a template. If `backend\.env`
already exists, edit it in place rather than overwriting it. Set
`GEMINI_API_KEY` to your own key to enable Gemini-backed bug analysis and
autonomous planning; do not commit or share that file. Without a key, the API
still starts, autonomous exploration uses its deterministic fallback, and
`/analyze-bug` returns a clear configuration error. The `GEMINI_MODEL` variable
is optional and defaults to `gemini-3.8-flash`.

`QAI_DATABASE_PATH` optionally selects the SQLite database file. Its default is
`backend\qai.db`.

`QAI_ALLOWED_HOSTS` optionally restricts website testing to comma-separated
hostnames, including wildcard suffixes such as `*.qa.example.com`. With no
allowlist configured, the initial public host is allowed and browser requests
may reach only that host. Private, loopback, reserved, and metadata addresses
are blocked. Only explicitly allowlisted public hosts may use non-default
ports. `QAI_MAX_CONCURRENT_WEBSITE_RUNS` controls concurrent jobs (1-8,
default 2).

## Run the API

From the project directory:

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.app:app --reload
```

The API is available at `http://127.0.0.1:8000`; interactive API documentation
is at `http://127.0.0.1:8000/docs`. Open the UI only through
`http://127.0.0.1:8000/ui/` (do not open `frontend\index.html` as a `file://`
URL). The first start creates `backend\.qai-api-token` with restricted local
access. It is never returned by the API or placed in frontend code. To use a
CLI client, read that file locally and send it as
`Authorization: Bearer <token>`; do not put it in a URL or commit it.

The API accepts only the configured `QAI_FRONTEND_ORIGINS` and
`QAI_ALLOWED_API_HOSTS` values, and uses an HttpOnly SameSite session for the
served UI. The default server binding is loopback. Do not bind to
`0.0.0.0` unless you have explicitly configured and reviewed the host,
origin, firewall, and deployment controls. A local process that can read the
token file can access the API.

## Website login test

Create a test run at `POST /test-runs` with platform `website` and mode
`user-directed`. Supply the login form CSS selectors and the rejection
indicator selector; an expected rejection-text substring and an
authenticated-state selector are optional. Start the run with
`POST /test-runs/{run_id}/start`. An optional JSON body supplies synthetic
credentials; otherwise the runner uses the non-routable `.invalid` username
and a clearly synthetic password. Credentials passed to the start endpoint
are not written to SQLite or logs.

Example run:

```powershell
$token = (Get-Content .\backend\.qai-api-token -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/test-runs `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body '{"run_name":"Invalid login check","platform":"website","mode":"user-directed","target":"https://your-authorized-test-site.example/login","goal":"Submit invalid test credentials and verify rejection.","expected_results":["The configured rejection message is visible."],"max_steps":10,"max_duration_seconds":120,"login_username_selector":"#username","login_password_selector":"#password","login_submit_selector":"button[type=''submit'']","rejection_selector":".error-message","rejection_text":"Invalid username or password"}'

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/test-runs/$($run.id)/start" `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body '{"username":"invalid-user@example.invalid","password":"synthetic-invalid-password"}'
```

Poll `GET /test-runs/{run_id}` for status and `GET
/test-runs/{run_id}/steps` for timestamped step outcomes. Request cancellation
with `POST /test-runs/{run_id}/cancel` and the same bearer header; cancellation takes effect at the end of
the current browser action. Screenshots are stored under `evidence\<run_id>\`
and their relative paths are included with the recorded step. The runner
performs inspection, username/password entry, submit, rejection and optional
authentication assertions, and a final screenshot.

## Bounded autonomous exploration

Create a website run with mode `autonomous-exploration`. It accepts the target
URL, exploration goal, maximum step count, timeout, and optional exact
additional hostnames in `allowed_hosts`. Autonomous runs are limited to 50
steps, 600 seconds, five top-level navigations, and five seconds per browser
action. Each inspected page and attempted action is saved as a test step.

Gemini is asked for one JSON action at a time. Plans are strictly validated
against observed element IDs and URLs. If planning is unavailable, times out,
or returns an invalid/repeated action, deterministic safe actions are used.
Only observed approved links, non-form search inputs, non-submit UI buttons
from a small safe-label list, and visibility assertions can be acted on.
Forms, password/secret fields, uploads, submits, purchase/delete/logout paths,
arbitrary selectors, and model-generated code are not executed. Page text is
sent to the planner only as untrusted observation data, never as instructions.

The global `QAI_ALLOWED_HOSTS` policy still applies. Per-run `allowed_hosts`
can add exact hostnames only, and they must also satisfy the global policy
when one is configured. Every navigation and browser request is validated;
private, loopback, reserved, local, and metadata targets are blocked, with
validated public DNS addresses pinned in Chromium. Local targets are not
enabled by this mode. Cancellation is honored between bounded browser
actions. Failed visibility assertions are reported as suspected bugs with
failure screenshots; browser, safety, and infrastructure errors are recorded
separately.

Choose **Bounded autonomous exploration** in the frontend form to use this
mode. Progress, cancellation, history, and report downloads use the existing
website-test dashboard.

## Execution history and reports

The frontend lists persisted runs with status/platform filters, pagination,
step details, and report download links. The matching API is
`GET /test-runs?status=completed&platform=website&limit=25&offset=0`.
Download reports for one UUID run ID with:

- `GET /test-runs/{run_id}/reports/excel` — `.xlsx` with `Test Summary`,
  `Test Steps`, `Bug Reports`, and `Execution Logs` worksheets.
- `GET /test-runs/{run_id}/reports/html` — escaped HTML containing the saved
  run summary, steps, assertion outcomes, errors, bug records, and logs.
- `GET /test-runs/{run_id}/steps/{step_number}/screenshot` — serves only
  screenshot evidence recorded for that step and confined to that run.

Reports are generated from SQLite records and saved under
`backend\generated_reports\<run_id>\`. Empty step, bug, or log collections are
shown as empty sections; no results are synthesized. Evidence is linked only
when a recorded path resolves to an existing file under the selected run's
`evidence\<run_id>\` directory. Common credential/token labels are redacted
from report text; test-run credentials are not persisted by the runner.

## Run tests

Install the test-only dependency and project packages:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Then run:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests use temporary SQLite databases and mock Gemini responses. Real
Chromium tests use intercepted local HTML fixtures and a browser smoke test;
no test contacts an external website. Milestone 5 also checks that rendered
Gemini output and provider error details cannot inject HTML or expose exception
text.
