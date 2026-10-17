import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from uuid import UUID

from dotenv import load_dotenv
from google.genai import types
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google import genai

from backend.database import (
    fail_interrupted_test_runs,
    get_dashboard_metrics,
    get_test_run_report_data,
    get_test_step_screenshot,
    get_test_run,
    initialize_database,
    list_test_run_history,
    list_test_steps,
    create_test_run as persist_test_run,
    update_test_run_status,
)
from backend.reports import (
    generate_excel_report,
    generate_html_report,
    resolve_evidence_path,
)
from backend.schemas import (
    RunPlatform,
    RunStatus,
    StartTestRunRequest,
    TestRun,
    TestRunCreate,
    TestRunHistoryPage,
    TestStep,
    DashboardMetrics,
)
from backend.website_runner import (
    TargetValidationError,
    build_login_action_plan,
    cancel_active_run,
    execute_website_run,
    execute_autonomous_website_run,
    register_active_run,
    release_run_slot,
    try_reserve_run,
    validate_target_url,
)


# ==================================================
# Load environment variables
# ==================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_FILE)

api_key = os.getenv("GEMINI_API_KEY")

from backend.security import (
    allowed_origins,
    bootstrap_session,
    validate_request_context,
)


# ==================================================
# Gemini Client
# ==================================================

client = genai.Client(api_key=api_key) if api_key else None

# Stable Gemini model
MODEL_NAME = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash"
)


# ==================================================
# FastAPI Application
# ==================================================

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    initialize_database()
    fail_interrupted_test_runs()
    yield


app = FastAPI(
    title="QAI - QA Copilot",
    version="1.0.0",
    description="AI-powered QA defect analysis assistant",
    lifespan=lifespan,
)
app.mount("/ui", StaticFiles(directory=os.path.join(BASE_DIR, "..", "frontend"), html=True), name="ui")


# ==================================================
# CORS
# ==================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins()),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ==================================================
# Request Model
# ==================================================

class BugRequest(BaseModel):
    observation: str


# ==================================================
# Home Route
# ==================================================

@app.get("/")
def home():
    return {
        "message": "QAI - QA Copilot API is running",
        "status": "online",
        "model": MODEL_NAME
    }


@app.post("/auth/session")
def create_browser_session(request: Request, response: Response):
    bootstrap_session(request, response)
    return {"success": True}


# ==================================================
# Test Run API
# ==================================================

@app.get("/test-runs", response_model=TestRunHistoryPage)
def read_test_run_history(
    request: Request,
    status: RunStatus | None = None,
    platform: RunPlatform | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    validate_request_context(request)
    items, total = list_test_run_history(
        limit=limit, offset=offset, status=status, platform=platform
    )
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@app.get("/dashboard/metrics", response_model=DashboardMetrics)
def read_dashboard_metrics(request: Request):
    validate_request_context(request)
    return get_dashboard_metrics()


@app.post("/test-runs", response_model=TestRun, status_code=201)
def create_test_run(request: Request, payload: TestRunCreate):
    validate_request_context(request, state_changing=True)
    return persist_test_run(payload)


@app.get("/test-runs/{run_id}", response_model=TestRun)
def read_test_run(run_id: str, request: Request):
    validate_request_context(request)
    run = get_test_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    return run


@app.get("/test-runs/{run_id}/steps", response_model=list[TestStep])
def read_test_steps(run_id: str, request: Request):
    validate_request_context(request)
    steps = list_test_steps(run_id)
    if steps is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    return steps


@app.post("/test-runs/{run_id}/start", response_model=TestRun, status_code=202)
def start_test_run(
    run_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    credentials: StartTestRunRequest | None = None,
):
    validate_request_context(request, state_changing=True)
    run = get_test_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    if run.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Test run cannot start from status '{run.status}'.",
        )
    if run.platform != "website":
        raise HTTPException(
            status_code=422,
            detail="This milestone supports website runs only.",
        )
    if run.mode not in {"user-directed", "autonomous-exploration"}:
        raise HTTPException(
            status_code=422,
            detail="This legacy test-run mode is not supported for execution.",
        )
    try:
        if run.mode == "user-directed":
            build_login_action_plan(run)
        validate_target_url(run.target)
    except (TargetValidationError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if not try_reserve_run():
        raise HTTPException(
            status_code=503,
            detail="Website test capacity is full. Try again later.",
        )

    try:
        started_run = update_test_run_status(run_id, "running")
        if started_run is None:
            raise HTTPException(status_code=404, detail="Test run not found.")
        cancel_event = threading.Event()
        register_active_run(run_id, cancel_event)
        if run.mode == "autonomous-exploration":
            background_tasks.add_task(
                execute_autonomous_website_run,
                run_id,
                cancel_event,
                planner=generate_autonomous_plan,
            )
        else:
            background_tasks.add_task(
                execute_website_run,
                run_id,
                credentials or StartTestRunRequest(),
                cancel_event,
            )
    except Exception:
        release_run_slot()
        raise

    return started_run


def generate_autonomous_plan(prompt: str) -> str:
    if client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text or ""


@app.post("/test-runs/{run_id}/cancel")
def cancel_test_run(run_id: str, request: Request):
    validate_request_context(request, state_changing=True)
    run = get_test_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    if run.status != "running" or not cancel_active_run(run_id):
        raise HTTPException(
            status_code=409,
            detail="Test run is not currently active.",
        )
    return {"success": True, "message": "Cancellation requested."}


@app.get("/test-runs/{run_id}/reports/excel")
def download_excel_report(run_id: UUID, request: Request):
    validate_request_context(request)
    run_key = str(run_id)
    report_data = get_test_run_report_data(run_key)
    if report_data is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    report_path = generate_excel_report(report_data)
    return FileResponse(
        report_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"qai-test-report-{run_key}.xlsx",
    )


@app.get("/test-runs/{run_id}/reports/html")
def download_html_report(run_id: UUID, request: Request):
    validate_request_context(request)
    run_key = str(run_id)
    report_data = get_test_run_report_data(run_key)
    if report_data is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    report_path = generate_html_report(report_data, str(request.base_url))
    return FileResponse(
        report_path,
        media_type="text/html; charset=utf-8",
        filename=f"qai-test-report-{run_key}.html",
    )


@app.get("/test-runs/{run_id}/steps/{step_number}/screenshot")
def download_step_screenshot(run_id: UUID, step_number: int, request: Request):
    validate_request_context(request)
    run_key = str(run_id)
    stored_path = get_test_step_screenshot(run_key, step_number)
    screenshot_path = resolve_evidence_path(run_key, stored_path)
    if screenshot_path is None:
        raise HTTPException(status_code=404, detail="Screenshot evidence not found.")
    return FileResponse(
        screenshot_path,
        media_type="image/png",
        filename=Path(screenshot_path).name,
    )


# ==================================================
# Analyze Bug
# ==================================================

@app.post("/analyze-bug")
def analyze_bug(request: Request, payload: BugRequest):
    validate_request_context(request, state_changing=True)

    observation = payload.observation.strip()

    # ----------------------------------------------
    # Validate input
    # ----------------------------------------------

    if not observation:
        return {
            "success": False,
            "error": "Bug observation cannot be empty."
        }

    if client is None:
        return {
            "success": False,
            "error": (
                "Gemini bug analysis is unavailable because "
                "GEMINI_API_KEY is not configured."
            )
        }

    # ----------------------------------------------
    # QA Prompt
    # ----------------------------------------------

    qa_prompt = f"""
You are an experienced software QA engineer.

Analyze the following tester observation:

"{observation}"

Generate a professional QA bug report using exactly these sections:

## Bug Title

## Module

## Description

## Steps to Reproduce

1.
2.
3.

## Expected Result

## Actual Result

## Severity

Give the severity and a short reason.

## Priority

Give the priority and a short reason.

## Possible Cause

List possible causes only.
Do not present possible causes as confirmed facts.

## Missing Information

List information that the tester should provide if it is unavailable.

Important rules:

- Do not invent information.
- If information is unavailable, write "Not provided".
- Keep the report clear and professional.
- Convert the tester's observation into proper QA language.
- Steps to reproduce must be numbered.
- Clearly distinguish facts from possible causes.
"""

    # ----------------------------------------------
    # Gemini request with retry
    # ----------------------------------------------

    max_retries = 3

    for attempt in range(max_retries):

        try:

            print(
                f"Gemini request: attempt "
                f"{attempt + 1}/{max_retries}"
            )

            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=qa_prompt
            )

            report = response.text

            if not report:
                return {
                    "success": False,
                    "error": "Gemini returned an empty response."
                }

            print("Gemini response received successfully.")

            return {
                "success": True,
                "report": report
            }

        except Exception as e:

            error_message = str(e)

            print(
                f"Gemini error on attempt "
                f"{attempt + 1} ({type(e).__name__})."
            )

            # --------------------------------------
            # Retry temporary 503 / 429 errors
            # --------------------------------------

            if (
                "503" in error_message
                or "UNAVAILABLE" in error_message
                or "429" in error_message
                or "RESOURCE_EXHAUSTED" in error_message
            ):

                if attempt < max_retries - 1:

                    wait_time = 2 ** attempt

                    print(
                        f"Temporary Gemini error. "
                        f"Retrying in {wait_time} seconds..."
                    )

                    time.sleep(wait_time)

                    continue

                return {
                    "success": False,
                    "error": (
                        "Gemini is temporarily unavailable "
                        "because of high demand. "
                        "Please try again in a few moments."
                    )
                }

            # --------------------------------------
            # Other errors
            # --------------------------------------

            return {
                "success": False,
                "error": f"Gemini request failed ({type(e).__name__})."
            }

    # ----------------------------------------------
    # Safety fallback
    # ----------------------------------------------

    return {
        "success": False,
        "error": "Unable to analyze the bug."
    }
