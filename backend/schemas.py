from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RunPlatform = Literal["website", "android"]
RunMode = Literal["user-directed", "autonomous-exploration", "autonomous"]
CreateRunMode = Literal["user-directed", "autonomous-exploration"]
RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
AssertionResult = Literal["passed", "failed", "not_evaluated"]
ExecutionError = Literal["browser", "safety", "timeout", "infrastructure"]
StepStatus = Literal["pending", "running", "passed", "failed", "error", "skipped"]
Severity = Literal["Critical", "High", "Medium", "Low"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TestRunCreate(Schema):
    run_name: str = Field(min_length=1, max_length=120)
    platform: RunPlatform
    mode: CreateRunMode
    target: str = Field(min_length=1, max_length=2048)
    goal: str = Field(min_length=1, max_length=10000)
    expected_results: list[str] = Field(default_factory=list, max_length=100)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)
    allow_form_submission: bool = False
    max_steps: int = Field(default=20, ge=1, le=1000)
    max_duration_seconds: int = Field(default=300, ge=1, le=3600)
    login_username_selector: str | None = Field(default=None, max_length=500)
    login_password_selector: str | None = Field(default=None, max_length=500)
    login_submit_selector: str | None = Field(default=None, max_length=500)
    rejection_selector: str | None = Field(default=None, max_length=500)
    rejection_text: str | None = Field(default=None, max_length=1000)
    authenticated_selector: str | None = Field(default=None, max_length=500)

    @field_validator("expected_results")
    @classmethod
    def expected_results_must_not_be_blank(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("Expected results cannot contain blank values.")
        return values

    @field_validator("allowed_hosts")
    @classmethod
    def allowed_hosts_must_be_valid(cls, values: list[str]) -> list[str]:
        from backend.website_runner import validate_allowed_hosts

        return validate_allowed_hosts(values)

    @field_validator(
        "login_username_selector",
        "login_password_selector",
        "login_submit_selector",
        "rejection_selector",
        "authenticated_selector",
    )
    @classmethod
    def selectors_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("Selectors cannot be blank.")
        return value

    @field_validator("rejection_text")
    @classmethod
    def rejection_text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("Rejection text cannot be blank.")
        return value

    @model_validator(mode="after")
    def validate_target_url(self):
        from backend.website_runner import validate_target_syntax

        validate_target_syntax(self.target)
        if self.mode == "autonomous-exploration":
            if self.platform != "website":
                raise ValueError("Autonomous exploration supports website runs only.")
            if self.max_steps > 50:
                raise ValueError("Autonomous exploration is limited to 50 steps.")
            if self.max_duration_seconds > 600:
                raise ValueError("Autonomous exploration is limited to 600 seconds.")
        return self


class TestRun(TestRunCreate):
    mode: RunMode
    id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_details: str | None = None
    assertion_result: AssertionResult = "not_evaluated"
    suspected_bug_count: int = Field(default=0, ge=0)
    assertion_passed_count: int = Field(default=0, ge=0)
    assertion_failed_count: int = Field(default=0, ge=0)
    execution_error: ExecutionError | None = None


class TestRunHistoryItem(Schema):
    id: UUID
    run_name: str
    platform: RunPlatform
    mode: RunMode
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    failure_summary: str | None = None
    assertion_result: AssertionResult = "not_evaluated"
    suspected_bug_count: int = Field(default=0, ge=0)
    assertion_passed_count: int = Field(default=0, ge=0)
    assertion_failed_count: int = Field(default=0, ge=0)
    execution_error: ExecutionError | None = None


class TestRunHistoryPage(Schema):
    items: list[TestRunHistoryItem]
    limit: int
    offset: int
    total: int


class DashboardMetrics(Schema):
    total_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    cancelled_runs: int = Field(ge=0)
    assertions_passed: int = Field(ge=0)
    assertions_failed: int = Field(ge=0)
    suspected_bugs: int = Field(ge=0)


class ExecutionLog(Schema):
    id: str
    run_id: str
    step_id: str | None = None
    timestamp: datetime
    event_type: str
    message: str


class TestStepCreate(Schema):
    step_number: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=2000)
    page_or_screen: str | None = Field(default=None, max_length=2048)
    expected_result: str | None = Field(default=None, max_length=5000)
    actual_result: str | None = Field(default=None, max_length=5000)
    status: StepStatus = "pending"
    duration_ms: int | None = Field(default=None, ge=0)
    error_details: str | None = Field(default=None, max_length=5000)
    screenshot_path: str | None = Field(default=None, max_length=2048)


class TestStep(TestStepCreate):
    id: str
    run_id: str
    created_at: datetime


class BugRecordCreate(Schema):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=10000)
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    reproduction_steps: list[str] = Field(default_factory=list, max_length=100)
    expected_result: str | None = Field(default=None, max_length=5000)
    actual_result: str | None = Field(default=None, max_length=5000)
    evidence_path: str | None = Field(default=None, max_length=2048)
    relevant_logs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("reproduction_steps", "relevant_logs")
    @classmethod
    def list_values_must_not_be_blank(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("List values cannot be blank.")
        return values


class WebsiteAction(Schema):
    action: Literal[
        "inspect_page",
        "fill_field",
        "click_control",
        "check_visibility",
        "verify_authentication",
        "capture_screenshot",
    ]
    selector: str | None = Field(default=None, max_length=500)
    value_source: Literal["username", "password"] | None = None
    expected_text: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_action_arguments(self):
        if self.action in {
            "fill_field",
            "click_control",
            "check_visibility",
            "verify_authentication",
        } and not self.selector:
            raise ValueError(f"{self.action} requires a selector.")

        if self.action == "fill_field" and self.value_source is None:
            raise ValueError("fill_field requires a username or password source.")

        if self.action != "fill_field" and self.value_source is not None:
            raise ValueError("Only fill_field may specify a value source.")

        if self.action not in {"check_visibility"} and self.expected_text is not None:
            raise ValueError("Only check_visibility may specify expected text.")

        if self.selector is not None and (
            self.selector.startswith(("xpath=", "text=", "//"))
        ):
            raise ValueError("Only CSS selectors are permitted.")

        return self


class AutonomousAction(Schema):
    action: Literal[
        "inspect", "click", "fill", "submit_form", "navigate", "assert_visible"
    ]
    element_id: str | None = Field(
        default=None, pattern=r"^(link|button|input|form|heading)-\d{1,4}$"
    )
    value: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=2048)
    expected_text: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_action_arguments(self):
        if self.action == "inspect":
            if any((self.element_id, self.value, self.url, self.expected_text)):
                raise ValueError("inspect does not accept action arguments.")
        elif self.action in {"click", "submit_form"}:
            if not self.element_id:
                raise ValueError(f"{self.action} requires an observed element ID.")
            if any((self.value, self.url, self.expected_text)):
                raise ValueError(
                    f"{self.action} accepts only an observed element ID."
                )
        elif self.action == "fill":
            if not self.element_id or not self.value:
                raise ValueError("fill requires an element ID and non-empty value.")
            if self.url is not None or self.expected_text is not None:
                raise ValueError("fill accepts only an element ID and value.")
        elif self.action == "navigate":
            if not self.url:
                raise ValueError("navigate requires an observed URL.")
            if any((self.element_id, self.value, self.expected_text)):
                raise ValueError("navigate accepts only an observed URL.")
        elif self.action == "assert_visible":
            if not self.element_id:
                raise ValueError("assert_visible requires an observed element ID.")
            if self.value is not None or self.url is not None:
                raise ValueError("assert_visible accepts an element ID and optional text.")
        return self


class AutonomousActionPlan(Schema):
    actions: list[AutonomousAction] = Field(min_length=1, max_length=1)


class StartTestRunRequest(Schema):
    username: str = Field(
        default="qai-invalid-user@example.invalid", min_length=1, max_length=320
    )
    password: str = Field(
        default="QAI-invalid-password-not-a-real-secret",
        min_length=1,
        max_length=1024,
    )


class BugRecord(BugRecordCreate):
    id: str
    run_id: str
    created_at: datetime
