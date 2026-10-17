import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from uuid import uuid4

from backend.schemas import (
    AssertionResult,
    ExecutionError,
    BugRecord,
    BugRecordCreate,
    ExecutionLog,
    RunStatus,
    TestRun,
    TestRunCreate,
    TestRunHistoryItem,
    TestStep,
    TestStepCreate,
)


_ASSERTION_ACTIONS = {
    "check_visibility",
    "verify_authentication",
    "autonomous:assert_visible",
}
_EXECUTION_ERROR_EVENTS: dict[str, ExecutionError] = {
    "browser_error": "browser",
    "safety_blocked": "safety",
    "runtime_limit": "timeout",
    "infrastructure_error": "infrastructure",
}


BACKEND_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(
    os.getenv("QAI_DATABASE_PATH", str(BACKEND_DIR / "qai.db"))
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs (
    id TEXT PRIMARY KEY,
    run_name TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('website', 'android')),
    mode TEXT NOT NULL CHECK (mode IN ('user-directed', 'autonomous')),
    target TEXT NOT NULL,
    goal TEXT NOT NULL,
    expected_results TEXT NOT NULL,
    max_steps INTEGER NOT NULL CHECK (max_steps BETWEEN 1 AND 1000),
    max_duration_seconds INTEGER NOT NULL
        CHECK (max_duration_seconds BETWEEN 1 AND 3600),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    error_details TEXT,
    login_username_selector TEXT,
    login_password_selector TEXT,
    login_submit_selector TEXT,
    rejection_selector TEXT,
    rejection_text TEXT,
    authenticated_selector TEXT,
    allow_form_submission INTEGER NOT NULL DEFAULT 0
        CHECK (allow_form_submission IN (0, 1)),
    autonomous_exploration INTEGER NOT NULL DEFAULT 0
        CHECK (autonomous_exploration IN (0, 1))
);

CREATE TABLE IF NOT EXISTS test_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL CHECK (step_number >= 1),
    action TEXT NOT NULL,
    page_or_screen TEXT,
    expected_result TEXT,
    actual_result TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'passed', 'failed', 'error', 'skipped')),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_details TEXT,
    screenshot_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, step_number)
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    step_id TEXT REFERENCES test_steps(id) ON DELETE SET NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bug_records (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('Critical', 'High', 'Medium', 'Low')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    reproduction_steps TEXT NOT NULL,
    expected_result TEXT,
    actual_result TEXT,
    evidence_path TEXT,
    relevant_logs TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_test_steps_run_id
    ON test_steps(run_id, step_number);
CREATE INDEX IF NOT EXISTS idx_bug_records_run_id
    ON bug_records(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_logs_run_id
    ON execution_logs(run_id, timestamp);
"""

_ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "failed", "cancelled"},
    "running": {"completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


@contextmanager
def _connection(
    database_path: str | Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    path = Path(database_path) if database_path is not None else DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def initialize_database(database_path: str | Path | None = None) -> None:
    with _connection(database_path) as connection:
        connection.executescript(_SCHEMA)
        _add_missing_columns(
            connection,
            "test_runs",
            {
                "started_at": "TEXT",
                "ended_at": "TEXT",
                "error_details": "TEXT",
                "login_username_selector": "TEXT",
                "login_password_selector": "TEXT",
                "login_submit_selector": "TEXT",
                "rejection_selector": "TEXT",
                "rejection_text": "TEXT",
                "authenticated_selector": "TEXT",
                "allow_form_submission": "INTEGER NOT NULL DEFAULT 0",
                "allowed_hosts": "TEXT NOT NULL DEFAULT '[]'",
                "autonomous_exploration": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _add_missing_columns(
            connection,
            "test_steps",
            {"screenshot_path": "TEXT"},
        )


def _add_missing_columns(
    connection: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }
    for column_name, column_type in columns.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )


def _test_run_from_row(
    row: sqlite3.Row | None,
    *,
    derived: dict[str, object] | None = None,
) -> TestRun | None:
    if row is None:
        return None

    values = dict(row)
    values["expected_results"] = json.loads(values["expected_results"])
    values["allowed_hosts"] = json.loads(values.get("allowed_hosts") or "[]")
    values["allow_form_submission"] = bool(values.get("allow_form_submission", 0))
    autonomous_exploration = values.pop("autonomous_exploration", 0)
    if values["mode"] == "autonomous" and autonomous_exploration:
        values["mode"] = "autonomous-exploration"
    if derived:
        values.update(derived)
    return TestRun.model_validate(values)


def _derived_result_fields(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    error_details: str | None,
) -> dict[str, object]:
    assertion_rows = connection.execute(
        """
        SELECT status FROM test_steps
        WHERE run_id = ? AND action IN (?, ?, ?)
        """,
        (run_id, *_ASSERTION_ACTIONS),
    ).fetchall()
    assertion_statuses = {row["status"] for row in assertion_rows}
    assertion_passed_count = sum(
        row["status"] == "passed" for row in assertion_rows
    )
    assertion_failed_count = sum(
        row["status"] == "failed" for row in assertion_rows
    )
    if "failed" in assertion_statuses:
        assertion_result: AssertionResult = "failed"
    elif assertion_statuses and assertion_statuses <= {"passed"}:
        assertion_result = "passed"
    else:
        assertion_result = "not_evaluated"

    bug_count = connection.execute(
        "SELECT COUNT(*) FROM bug_records WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    execution_error: ExecutionError | None = None
    if status == "failed":
        event_rows = connection.execute(
            """
            SELECT event_type FROM execution_logs
            WHERE run_id = ?
            ORDER BY timestamp DESC, id DESC
            """,
            (run_id,),
        ).fetchall()
        for row in event_rows:
            execution_error = _EXECUTION_ERROR_EVENTS.get(row["event_type"])
            if execution_error:
                break
    if execution_error is None and status == "failed" and error_details:
        if "timeout" in error_details.casefold() or "runtime limit" in error_details.casefold():
            execution_error = "timeout"
        elif "unsafe" in error_details.casefold() or "navigation" in error_details.casefold():
            execution_error = "safety"
        else:
            execution_error = "infrastructure"

    return {
        "assertion_result": assertion_result,
        "suspected_bug_count": bug_count,
        "assertion_passed_count": assertion_passed_count,
        "assertion_failed_count": assertion_failed_count,
        "execution_error": execution_error,
    }


def get_dashboard_metrics(
    database_path: str | Path | None = None,
) -> dict[str, int]:
    with _connection(database_path) as connection:
        status_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM test_runs GROUP BY status"
            )
        }
        assertion_counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM test_steps
            WHERE action IN (?, ?, ?)
            """,
            tuple(_ASSERTION_ACTIONS),
        ).fetchone()
        suspected_bugs = connection.execute(
            "SELECT COUNT(*) FROM bug_records"
        ).fetchone()[0]
    return {
        "total_runs": sum(status_counts.values()),
        "completed_runs": status_counts.get("completed", 0),
        "failed_runs": status_counts.get("failed", 0),
        "cancelled_runs": status_counts.get("cancelled", 0),
        "assertions_passed": assertion_counts["passed"] or 0,
        "assertions_failed": assertion_counts["failed"] or 0,
        "suspected_bugs": suspected_bugs,
    }


def _test_step_from_row(row: sqlite3.Row) -> TestStep:
    return TestStep.model_validate(dict(row))


def _bug_record_from_row(row: sqlite3.Row) -> BugRecord:
    values = dict(row)
    values["reproduction_steps"] = json.loads(values["reproduction_steps"])
    values["relevant_logs"] = json.loads(values["relevant_logs"])
    return BugRecord.model_validate(values)


def create_test_run(
    run: TestRunCreate, database_path: str | Path | None = None
) -> TestRun:
    run_id = str(uuid4())
    now = _utc_now()
    run_data = run.model_dump(mode="json")
    autonomous_exploration = int(
        run_data["mode"] == "autonomous-exploration"
    )
    stored_mode = "autonomous" if autonomous_exploration else run_data["mode"]
    values = {
        **run_data,
        "id": run_id,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }

    with _connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO test_runs (
                id, run_name, platform, mode, target, goal, expected_results,
                max_steps, max_duration_seconds, status, created_at, updated_at,
                login_username_selector, login_password_selector,
                login_submit_selector, rejection_selector, rejection_text,
                authenticated_selector, allowed_hosts, allow_form_submission,
                autonomous_exploration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run_data["run_name"],
                run_data["platform"],
                stored_mode,
                run_data["target"],
                run_data["goal"],
                json.dumps(run_data["expected_results"]),
                run_data["max_steps"],
                run_data["max_duration_seconds"],
                "pending",
                now.isoformat(),
                now.isoformat(),
                run_data["login_username_selector"],
                run_data["login_password_selector"],
                run_data["login_submit_selector"],
                run_data["rejection_selector"],
                run_data["rejection_text"],
                run_data["authenticated_selector"],
                json.dumps(run_data["allowed_hosts"]),
                int(run_data["allow_form_submission"]),
                autonomous_exploration,
            ),
        )

    return TestRun.model_validate(values)


def get_test_run(
    run_id: str, database_path: str | Path | None = None
) -> TestRun | None:
    with _connection(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        derived = (
            _derived_result_fields(
                connection,
                run_id,
                status=row["status"],
                error_details=row["error_details"],
            )
            if row is not None
            else None
        )
    return _test_run_from_row(row, derived=derived)


def update_test_run_status(
    run_id: str,
    status: RunStatus,
    database_path: str | Path | None = None,
) -> TestRun | None:
    with _connection(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None

        current_status = row["status"]
        if status not in _ALLOWED_STATUS_TRANSITIONS[current_status]:
            raise ValueError(
                f"Cannot change test run status from "
                f"'{current_status}' to '{status}'."
            )

        connection.execute(
            """
            UPDATE test_runs
            SET status = ?, updated_at = ?,
                started_at = CASE WHEN ? = 'running' THEN ? ELSE started_at END,
                ended_at = CASE WHEN ? IN ('completed', 'failed', 'cancelled')
                    THEN ? ELSE ended_at END
            WHERE id = ?
            """,
            (
                status,
                _utc_now().isoformat(),
                status,
                _utc_now().isoformat(),
                status,
                _utc_now().isoformat(),
                run_id,
            ),
        )
        updated_row = connection.execute(
            "SELECT * FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        timestamp = _utc_now().isoformat()
        connection.execute(
            """
            INSERT INTO execution_logs
                (id, run_id, timestamp, event_type, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                run_id,
                timestamp,
                "run_status",
                f"Run status changed to {status}.",
            ),
        )
        derived = _derived_result_fields(
            connection,
            run_id,
            status=updated_row["status"],
            error_details=updated_row["error_details"],
        )
    return _test_run_from_row(updated_row, derived=derived)


def add_test_step(
    run_id: str,
    step: TestStepCreate,
    database_path: str | Path | None = None,
) -> TestStep:
    step_id = str(uuid4())
    created_at = _utc_now()
    step_data = step.model_dump(mode="json")

    with _connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO test_steps (
                id, run_id, step_number, action, page_or_screen,
                expected_result, actual_result, status, duration_ms,
                error_details, screenshot_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_id,
                run_id,
                step_data["step_number"],
                step_data["action"],
                step_data["page_or_screen"],
                step_data["expected_result"],
                step_data["actual_result"],
                step_data["status"],
                step_data["duration_ms"],
                step_data["error_details"],
                step_data["screenshot_path"],
                created_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO execution_logs
                (id, run_id, step_id, timestamp, event_type, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                run_id,
                step_id,
                created_at.isoformat(),
                "step_result",
                f"Step {step_data['step_number']} recorded with status "
                f"{step_data['status']}.",
            ),
        )

    return TestStep.model_validate(
        {
            **step_data,
            "id": step_id,
            "run_id": run_id,
            "created_at": created_at,
        }
    )


def add_execution_log(
    run_id: str,
    event_type: str,
    message: str,
    *,
    step_id: str | None = None,
    database_path: str | Path | None = None,
) -> None:
    with _connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO execution_logs
                (id, run_id, step_id, timestamp, event_type, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                run_id,
                step_id,
                _utc_now().isoformat(),
                event_type,
                message[:2000],
            ),
        )


def list_test_steps(
    run_id: str, database_path: str | Path | None = None
) -> list[TestStep] | None:
    with _connection(database_path) as connection:
        run_exists = connection.execute(
            "SELECT 1 FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run_exists is None:
            return None

        rows = connection.execute(
            """
            SELECT * FROM test_steps
            WHERE run_id = ?
            ORDER BY step_number
            """,
            (run_id,),
        ).fetchall()
    return [_test_step_from_row(row) for row in rows]


def set_test_run_error(
    run_id: str,
    error_details: str,
    database_path: str | Path | None = None,
) -> TestRun | None:
    with _connection(database_path) as connection:
        connection.execute(
            """
            UPDATE test_runs
            SET error_details = ?, updated_at = ?, ended_at = ?
            WHERE id = ?
            """,
            (error_details[:5000], _utc_now().isoformat(), _utc_now().isoformat(), run_id),
        )
        connection.execute(
            """
            INSERT INTO execution_logs
                (id, run_id, timestamp, event_type, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                run_id,
                _utc_now().isoformat(),
                "run_error",
                "Run encountered an execution error.",
            ),
        )
        row = connection.execute(
            "SELECT * FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _test_run_from_row(row)


def fail_interrupted_test_runs(
    database_path: str | Path | None = None,
) -> None:
    now = _utc_now().isoformat()
    with _connection(database_path) as connection:
        connection.execute(
            """
            UPDATE test_runs
            SET status = 'failed',
                error_details = 'Execution was interrupted by a backend restart.',
                updated_at = ?,
                ended_at = ?
            WHERE status = 'running'
            """,
            (now, now),
        )


def add_bug_record(
    run_id: str,
    bug: BugRecordCreate,
    database_path: str | Path | None = None,
) -> BugRecord:
    bug_id = str(uuid4())
    created_at = _utc_now()
    bug_data = bug.model_dump(mode="json")

    with _connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO bug_records (
                id, run_id, title, description, severity, confidence,
                reproduction_steps, expected_result, actual_result,
                evidence_path, relevant_logs, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bug_id,
                run_id,
                bug_data["title"],
                bug_data["description"],
                bug_data["severity"],
                bug_data["confidence"],
                json.dumps(bug_data["reproduction_steps"]),
                bug_data["expected_result"],
                bug_data["actual_result"],
                bug_data["evidence_path"],
                json.dumps(bug_data["relevant_logs"]),
                created_at.isoformat(),
            ),
        )

    return BugRecord.model_validate(
        {
            **bug_data,
            "id": bug_id,
            "run_id": run_id,
            "created_at": created_at,
        }
    )


def list_bug_records(
    run_id: str, database_path: str | Path | None = None
) -> list[BugRecord] | None:
    with _connection(database_path) as connection:
        run_exists = connection.execute(
            "SELECT 1 FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run_exists is None:
            return None

        rows = connection.execute(
            """
            SELECT * FROM bug_records
            WHERE run_id = ?
            ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()
    return [_bug_record_from_row(row) for row in rows]


def list_test_run_history(
    *,
    limit: int = 25,
    offset: int = 0,
    status: str | None = None,
    platform: str | None = None,
    database_path: str | Path | None = None,
) -> tuple[list[TestRunHistoryItem], int]:
    filters: list[str] = []
    parameters: list[object] = []
    if status is not None:
        filters.append("r.status = ?")
        parameters.append(status)
    if platform is not None:
        filters.append("r.platform = ?")
        parameters.append(platform)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    with _connection(database_path) as connection:
        connection.execute("BEGIN")
        total = connection.execute(
            f"SELECT COUNT(*) FROM test_runs r {where_clause}", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT r.id, r.run_name, r.platform,
                   CASE WHEN r.mode = 'autonomous' AND r.autonomous_exploration = 1
                        THEN 'autonomous-exploration'
                        ELSE r.mode END AS mode,
                   r.status, r.created_at,
                   r.started_at, r.ended_at,
                   CASE WHEN r.started_at IS NOT NULL AND r.ended_at IS NOT NULL
                        THEN MAX(0, CAST(
                            (julianday(r.ended_at) - julianday(r.started_at))
                            * 86400000 AS INTEGER
                        ))
                        ELSE NULL END AS duration_ms,
                   COALESCE(
                       r.error_details,
                       (
                           SELECT COALESCE(s.error_details, s.actual_result)
                           FROM test_steps s
                           WHERE s.run_id = r.id AND s.status IN ('failed', 'error')
                           ORDER BY s.step_number
                           LIMIT 1
                       )
                   ) AS failure_summary
            FROM test_runs r
            {where_clause}
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        ).fetchall()
        history_items = []
        for row in rows:
            values = dict(row)
            values.update(
                _derived_result_fields(
                    connection,
                    values["id"],
                    status=values["status"],
                    error_details=values.get("failure_summary"),
                )
            )
            history_items.append(TestRunHistoryItem.model_validate(values))
    return history_items, total


def get_test_run_report_data(
    run_id: str, database_path: str | Path | None = None
) -> dict[str, object] | None:
    with _connection(database_path) as connection:
        connection.execute("BEGIN")
        run_row = connection.execute(
            "SELECT * FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            return None
        step_rows = connection.execute(
            """
            SELECT * FROM test_steps
            WHERE run_id = ?
            ORDER BY step_number
            """,
            (run_id,),
        ).fetchall()
        bug_rows = connection.execute(
            """
            SELECT * FROM bug_records
            WHERE run_id = ?
            ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()
        log_rows = connection.execute(
            """
            SELECT id, run_id, step_id, timestamp, event_type, message
            FROM execution_logs
            WHERE run_id = ?
            ORDER BY timestamp, id
            """,
            (run_id,),
        ).fetchall()
        derived = _derived_result_fields(
            connection,
            run_id,
            status=run_row["status"],
            error_details=run_row["error_details"],
        )
        run = _test_run_from_row(run_row, derived=derived)
    return {
        "run": run,
        "steps": [_test_step_from_row(row) for row in step_rows],
        "bugs": [_bug_record_from_row(row) for row in bug_rows],
        "logs": [ExecutionLog.model_validate(dict(row)) for row in log_rows],
    }


def get_test_step_screenshot(
    run_id: str, step_number: int, database_path: str | Path | None = None
) -> str | None:
    with _connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT screenshot_path FROM test_steps
            WHERE run_id = ? AND step_number = ?
            """,
            (run_id, step_number),
        ).fetchone()
    return row["screenshot_path"] if row is not None else None
