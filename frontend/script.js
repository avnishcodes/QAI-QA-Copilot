const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

const analyzeBtn = document.getElementById("analyzeBtn");
const bugInput = document.getElementById("bugInput");
const result = document.getElementById("result");
const loading = document.getElementById("loading");
const newAnalysisBtn = document.getElementById("newAnalysisBtn");
const activeReportMeta = document.getElementById("activeReportMeta");
const copyReportBtn = document.getElementById("copyReportBtn");
const copyBtnText = document.getElementById("copyBtnText");
const refreshSavedAnalysesBtn = document.getElementById("refreshSavedAnalysesBtn");
const savedAnalysesStatus = document.getElementById("savedAnalysesStatus");
const savedAnalysesList = document.getElementById("savedAnalysesList");

let currentActiveReportMarkdown = "";
let currentActiveReportId = null;
const websiteRunForm = document.getElementById("websiteRunForm");
const runStatus = document.getElementById("runStatus");
const runSteps = document.getElementById("runSteps");
const startRunBtn = document.getElementById("startRunBtn");
const cancelRunBtn = document.getElementById("cancelRunBtn");
const runModeSelect = document.getElementById("runMode");
const historyStatusSelect = document.getElementById("historyStatus");
const historyPlatformSelect = document.getElementById("historyPlatform");
const historySearch = document.getElementById("historySearch");
const historyList = document.getElementById("historyList");
const historyMessage = document.getElementById("historyStatusMessage");
const historyDetails = document.getElementById("historyDetails");
const historyPreviousBtn = document.getElementById("historyPreviousBtn");
const historyNextBtn = document.getElementById("historyNextBtn");
const historyPageLabel = document.getElementById("historyPageLabel");
const HISTORY_PAGE_SIZE = 25;

let activeRunId = null;
let cancelRequested = false;
let historyOffset = 0;
let historyTotal = 0;
let currentHistoryItems = [];

const pageTitles = {
    overview: "Overview",
    "new-run": "New Test Run",
    history: "Execution History",
    analysis: "AI Bug Analysis",
    reports: "Reports"
};

for (const navigationItem of document.querySelectorAll("[data-page]")) {
    navigationItem.addEventListener("click", (event) => {
        event.preventDefault();
        setActivePage(navigationItem.dataset.page);
    });
}

function setActivePage(page) {
    const panelPage = page === "analysis" ? "overview" : page;
    for (const panel of document.querySelectorAll("[data-page-panel]")) {
        const isActive = panel.dataset.pagePanel === panelPage;
        panel.hidden = !isActive;
        panel.classList.toggle("active", isActive);
    }
    document.getElementById("overviewSection").classList.toggle("analysis-focused", page === "analysis");
    for (const navigationItem of document.querySelectorAll(".nav-item[data-page]")) {
        const isActive = navigationItem.dataset.page === page;
        navigationItem.classList.toggle("active", isActive);
        if (isActive) navigationItem.setAttribute("aria-current", "page");
        else navigationItem.removeAttribute("aria-current");
    }
    document.getElementById("pageTitle").textContent = pageTitles[page] || pageTitles.overview;
    if (page === "analysis") {
        loadSavedAnalyses();
        document.getElementById("bugAnalysisCard").scrollIntoView({ block: "start", behavior: "smooth" });
        bugInput.focus({ preventScroll: true });
    }
    if (page === "new-run") updateRunModeFields();
    if (page === "history") loadExecutionHistory();
    if (page === "overview") loadDashboardSummary();
}

function formatApiErrorMessage(data, status) {
    if (!data) return `Request failed with status ${status}.`;
    if (typeof data.detail === "string" && data.detail.trim()) {
        return data.detail.trim();
    }
    if (Array.isArray(data.detail)) {
        const parts = data.detail
            .map((err) => {
                if (typeof err === "string") return err;
                if (err && typeof err === "object") {
                    const loc = Array.isArray(err.loc)
                        ? err.loc.filter((p) => p !== "body").join(".")
                        : "";
                    const msg = err.msg || err.message || "";
                    return loc && msg ? `${loc}: ${msg}` : (msg || JSON.stringify(err));
                }
                return String(err);
            })
            .filter(Boolean);
        if (parts.length) return parts.join("; ");
    } else if (data.detail && typeof data.detail === "object") {
        const msg = data.detail.msg || data.detail.message || data.detail.error;
        if (typeof msg === "string" && msg.trim()) return msg.trim();
    }
    if (typeof data.error === "string" && data.error.trim()) {
        return data.error.trim();
    }
    if (typeof data.message === "string" && data.message.trim()) {
        return data.message.trim();
    }
    return `Request failed with status ${status}.`;
}

async function requestJson(path, options = {}) {
    const fetchOptions = { credentials: "same-origin", ...options };
    const headers = new Headers(options.headers || {});
    if (options.body && typeof options.body === "string" && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
    }
    fetchOptions.headers = headers;

    const response = await fetch(`${API_BASE}${path}`, fetchOptions);
    const contentType = response.headers.get("content-type") || "";
    let data = null;
    if (contentType.includes("application/json")) {
        try {
            data = await response.json();
        } catch {
            data = null;
        }
    }
    if (!response.ok) {
        let message = formatApiErrorMessage(data, response.status);
        if (!data) {
            try {
                const text = (await response.text()).trim();
                if (text) message = text.slice(0, 300);
            } catch {
                // Keep default message
            }
        }
        throw new Error(message);
    }
    if (data === null) {
        data = await response.json();
    }
    return data;
}

async function bootstrapSession() {
    const response = await fetch("/auth/session", {
        method: "POST",
        credentials: "same-origin"
    });
    if (!response.ok) throw new Error("Secure local API session could not be established.");
}

function setResultMessage(message, className) {
    const paragraph = document.createElement("p");
    paragraph.className = className;
    paragraph.textContent = message;
    result.replaceChildren(paragraph);
}

async function loadSavedAnalyses() {
    if (!savedAnalysesList) return;
    try {
        if (savedAnalysesStatus) savedAnalysesStatus.textContent = "Loading saved analyses…";
        const data = await requestJson("/ai-bug-reports?limit=20");
        if (savedAnalysesStatus) savedAnalysesStatus.textContent = "";
        renderSavedAnalyses(data.items || []);
    } catch (error) {
        console.error("Failed to load saved analyses:", error);
        if (savedAnalysesStatus) savedAnalysesStatus.textContent = "Could not load saved analyses.";
    }
}

function renderSavedAnalyses(items) {
    if (!savedAnalysesList) return;
    savedAnalysesList.replaceChildren();
    if (!items.length) {
        const empty = document.createElement("li");
        empty.className = "saved-analysis-empty";
        empty.textContent = "No saved analyses yet. Generated reports will be stored here.";
        savedAnalysesList.append(empty);
        return;
    }

    for (const item of items) {
        const li = document.createElement("li");
        li.className = "saved-analysis-item";
        li.dataset.reportId = item.id;
        if (item.id === currentActiveReportId) {
            li.classList.add("active");
        }

        const titleDiv = document.createElement("div");
        titleDiv.className = "saved-analysis-title";
        titleDiv.textContent = item.title || "Untitled Bug Report";

        const metaDiv = document.createElement("div");
        metaDiv.className = "saved-analysis-meta";
        const dateSpan = document.createElement("span");
        const dateObj = new Date(item.created_at);
        dateSpan.textContent = isNaN(dateObj.getTime())
            ? item.created_at
            : dateObj.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
        metaDiv.append(dateSpan);

        if (item.run_id) {
            const runSpan = document.createElement("span");
            runSpan.className = "status-badge";
            runSpan.textContent = "Linked Run";
            metaDiv.append(runSpan);
        }

        li.append(titleDiv, metaDiv);
        li.addEventListener("click", () => {
            selectSavedAnalysis(item);
        });
        savedAnalysesList.append(li);
    }
}

function selectSavedAnalysis(item) {
    currentActiveReportId = item.id;
    currentActiveReportMarkdown = item.report_markdown || "";

    bugInput.value = item.observation || "";
    result.replaceChildren(formatReport(item.report_markdown));

    if (activeReportMeta) {
        activeReportMeta.replaceChildren();
        const dateObj = new Date(item.created_at);
        const formattedDate = isNaN(dateObj.getTime()) ? item.created_at : dateObj.toLocaleString();
        const metaText = document.createElement("span");
        metaText.textContent = `Saved: ${formattedDate} (${item.model || "gemini"})`;
        activeReportMeta.append(metaText);
        if (item.run_id) {
            const runLink = document.createElement("a");
            runLink.href = "#";
            runLink.className = "inline-link";
            runLink.style.marginLeft = "8px";
            runLink.textContent = `· View linked run (${item.run_id.slice(0, 8)}…)`;
            runLink.addEventListener("click", (e) => {
                e.preventDefault();
                loadHistoricalRun(item.run_id);
            });
            activeReportMeta.append(runLink);
        }
    }

    if (copyReportBtn) {
        copyReportBtn.classList.remove("hidden");
    }

    for (const li of savedAnalysesList.querySelectorAll(".saved-analysis-item")) {
        li.classList.toggle("active", li.dataset.reportId === item.id);
    }
}

function clearActiveAnalysis() {
    currentActiveReportId = null;
    currentActiveReportMarkdown = "";
    bugInput.value = "";
    setResultMessage("Your AI-generated QA report will appear here.", "placeholder");
    if (activeReportMeta) activeReportMeta.textContent = "";
    if (copyReportBtn) copyReportBtn.classList.add("hidden");
    if (savedAnalysesList) {
        for (const li of savedAnalysesList.querySelectorAll(".saved-analysis-item")) {
            li.classList.remove("active");
        }
    }
    bugInput.focus();
}

async function copyReportMarkdown() {
    if (!currentActiveReportMarkdown) return;
    try {
        await navigator.clipboard.writeText(currentActiveReportMarkdown);
        if (copyBtnText) copyBtnText.textContent = "Copied!";
        setTimeout(() => {
            if (copyBtnText) copyBtnText.textContent = "Copy Markdown";
        }, 2000);
    } catch (err) {
        console.error("Clipboard copy failed:", err);
    }
}

if (newAnalysisBtn) newAnalysisBtn.addEventListener("click", clearActiveAnalysis);
if (copyReportBtn) copyReportBtn.addEventListener("click", copyReportMarkdown);
if (refreshSavedAnalysesBtn) refreshSavedAnalysesBtn.addEventListener("click", loadSavedAnalyses);

analyzeBtn.addEventListener("click", async () => {
    const observation = bugInput.value.trim();
    if (!observation) {
        bugInput.setCustomValidity("Please describe the bug first.");
        bugInput.reportValidity();
        bugInput.addEventListener("input", () => bugInput.setCustomValidity(""), { once: true });
        return;
    }

    loading.classList.remove("hidden");
    setResultMessage("Analyzing bug with AI…", "placeholder");
    analyzeBtn.disabled = true;
    analyzeBtn.textContent = "Analyzing…";

    try {
        const response = await fetch(`${API_BASE}/analyze-bug`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ observation }),
            credentials: "same-origin"
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `Request failed with status ${response.status}.`);
        if (data.success === true) {
            currentActiveReportMarkdown = data.report;
            result.replaceChildren(formatReport(data.report));
            if (activeReportMeta) {
                activeReportMeta.textContent = "Generated just now";
            }
            if (copyReportBtn) {
                copyReportBtn.classList.remove("hidden");
            }
            loadSavedAnalyses();
        } else {
            setResultMessage(`Error: ${data.error || "Unknown error"}`, "error-message");
        }
    } catch (error) {
        console.error("Bug analysis request failed:", error);
        const message = error instanceof TypeError
            ? "Could not connect to the QA backend. Make sure FastAPI is running."
            : `Bug analysis failed: ${error.message}`;
        setResultMessage(message, "error-message");
    } finally {
        loading.classList.add("hidden");
        analyzeBtn.disabled = false;
        analyzeBtn.replaceChildren(
            document.createTextNode("Analyze bug "),
            createIcon("M5 12h14m-6-6 6 6-6 6")
        );
    }
});

function createIcon(pathData) {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "icon");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    icon.append(path);
    return icon;
}

function formatReport(report) {
    const lines = String(report ?? "").split(/\r?\n/);
    const output = document.createDocumentFragment();
    let activeList = null;
    let listElement = null;

    function closeList() {
        activeList = null;
        listElement = null;
    }

    function appendTextElement(tagName, text, parent = output) {
        const element = document.createElement(tagName);
        element.textContent = text;
        parent.append(element);
    }

    for (const line of lines) {
        const heading = line.match(/^\s*(#{2,3})\s+(.+?)\s*$/);
        const listItem = line.match(/^\s*(\d+[.)]|[-*])\s+(.+?)\s*$/);
        const boldHeading = line.match(/^\s*\*\*(.+?)\*\*\s*$/);

        if (heading) {
            closeList();
            const tag = heading[1].length === 2 ? "h3" : "h4";
            appendTextElement(tag, heading[2]);
        } else if (boldHeading) {
            closeList();
            appendTextElement("h4", boldHeading[1]);
        } else if (listItem) {
            const listTag = /^\d/.test(listItem[1]) ? "ol" : "ul";
            if (activeList !== listTag) {
                closeList();
                listElement = document.createElement(listTag);
                output.append(listElement);
                activeList = listTag;
            }
            appendTextElement("li", listItem[2], listElement);
        } else if (!line.trim()) {
            closeList();
        } else {
            closeList();
            appendTextElement("p", line);
        }
    }
    closeList();
    return output;
}

function updateRunModeFields() {
    if (!websiteRunForm || !runModeSelect) return;
    const loginMode = runModeSelect.value === "user-directed";
    const loginSettings = document.querySelector(".login-settings");
    if (loginSettings) {
        loginSettings.classList.toggle("hidden", !loginMode);
    }
    const formSubmissionField = document.querySelector(".autonomous-only-field");
    if (formSubmissionField) {
        formSubmissionField.classList.toggle("hidden", loginMode);
    }
    const allowFormSubmission = document.getElementById("allowFormSubmission");
    if (allowFormSubmission) {
        allowFormSubmission.disabled = loginMode;
    }

    const requiredLoginIds = new Set([
        "loginUsernameSelector",
        "loginPasswordSelector",
        "loginSubmitSelector",
        "rejectionSelector",
        "testUsername",
        "testPassword"
    ]);

    for (const field of document.querySelectorAll(".login-only-field")) {
        field.classList.toggle("hidden", !loginMode);
        const input = field.querySelector("input");
        if (input) {
            input.disabled = !loginMode;
            const isRequired = loginMode && requiredLoginIds.has(input.id);
            input.required = isRequired;
            if (isRequired) {
                input.setAttribute("required", "");
            } else {
                input.removeAttribute("required");
            }
        }
    }
    const maxSteps = document.getElementById("runMaxSteps");
    const timeout = document.getElementById("runTimeout");
    if (maxSteps) {
        maxSteps.min = loginMode ? "6" : "1";
        maxSteps.max = loginMode ? "1000" : "50";
        if (!loginMode && Number(maxSteps.value) > 50) {
            maxSteps.value = "20";
        }
    }
    if (timeout) {
        timeout.max = loginMode ? "3600" : "600";
        if (!loginMode && Number(timeout.value) > 600) {
            timeout.value = "120";
        }
    }
}

if (websiteRunForm) {
    runModeSelect.addEventListener("change", updateRunModeFields);
    runModeSelect.addEventListener("input", updateRunModeFields);
    websiteRunForm.addEventListener("reset", () => {
        setTimeout(updateRunModeFields, 0);
    });
    updateRunModeFields();

    websiteRunForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        setActivePage("new-run");
        startRunBtn.disabled = true;
        cancelRunBtn.classList.add("hidden");
        runStatus.textContent = "Creating website test run…";
        runSteps.replaceChildren();

        const expectedResults = document.getElementById("runExpected").value
            .split(/\r?\n/)
            .map((item) => item.trim())
            .filter(Boolean);

        const payload = {
            run_name: document.getElementById("runName").value.trim(),
            platform: "website",
            mode: runModeSelect.value,
            target: document.getElementById("runTarget").value.trim(),
            goal: document.getElementById("runGoal").value.trim(),
            expected_results: expectedResults,
            allowed_hosts: document.getElementById("runAllowedHosts").value
                .split(",")
                .map((host) => host.trim())
                .filter(Boolean),
            max_steps: Number(document.getElementById("runMaxSteps").value),
            max_duration_seconds: Number(document.getElementById("runTimeout").value),
            allow_form_submission: runModeSelect.value === "autonomous-exploration"
                && document.getElementById("allowFormSubmission").checked
        };
        if (payload.mode === "user-directed") {
            Object.assign(payload, {
                login_username_selector: document.getElementById("loginUsernameSelector").value.trim(),
                login_password_selector: document.getElementById("loginPasswordSelector").value.trim(),
                login_submit_selector: document.getElementById("loginSubmitSelector").value.trim(),
                rejection_selector: document.getElementById("rejectionSelector").value.trim(),
                rejection_text: document.getElementById("rejectionText").value.trim() || null,
                authenticated_selector: document.getElementById("authenticatedSelector").value.trim() || null
            });
        }

        try {
            const created = await requestJson("/test-runs", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            activeRunId = created.id;
            runStatus.textContent = `Run ${created.id}: starting…`;
            const credentials = payload.mode === "user-directed"
                ? {
                    username: document.getElementById("testUsername").value.trim(),
                    password: document.getElementById("testPassword").value
                }
                : null;
            const startOptions = credentials
                ? {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(credentials)
                }
                : { method: "POST" };
            let started;
            try {
                started = await requestJson(
                    `/test-runs/${encodeURIComponent(activeRunId)}/start`,
                    startOptions
                );
            } finally {
                if (credentials) document.getElementById("testPassword").value = "";
            }

            cancelRequested = false;
            cancelRunBtn.classList.remove("hidden");
            await pollTestRun(activeRunId);
            if (started.status === "running") await loadDashboardSummary();
        } catch (error) {
            runStatus.textContent = `Error: ${error.message}`;
        } finally {
            activeRunId = null;
            startRunBtn.disabled = false;
            cancelRunBtn.classList.add("hidden");
            await Promise.all([loadExecutionHistory(), loadDashboardSummary()]);
        }
    });

    cancelRunBtn.addEventListener("click", async () => {
        if (!activeRunId || cancelRequested) return;
        cancelRequested = true;
        cancelRunBtn.disabled = true;
        try {
            const data = await requestJson(
                `/test-runs/${encodeURIComponent(activeRunId)}/cancel`,
                { method: "POST" }
            );
            runStatus.textContent = data.message || "Cancellation requested.";
        } catch (error) {
            cancelRequested = false;
            runStatus.textContent = `Error: ${error.message}`;
        } finally {
            cancelRunBtn.disabled = false;
        }
    });
}

function createStatusBadge(status) {
    const badge = document.createElement("span");
    badge.className = "status-badge";
    badge.dataset.status = status;
    badge.textContent = status;
    return badge;
}

function assertionResultLabel(run) {
    if (run.assertion_result === "passed") return "Assertions passed";
    if (run.assertion_result === "failed") return "Assertion failure detected";
    return "No assertions evaluated";
}

function executionStatusLabel(run) {
    if (run.status === "completed") return "Execution completed";
    if (run.status === "failed") return "Execution failed";
    if (run.status === "cancelled") return "Execution cancelled";
    if (run.status === "running") return "Execution running";
    return "Execution pending";
}

function appendRunOutcome(cell, item) {
    const outcome = document.createElement("small");
    outcome.className = "table-run-id";
    outcome.textContent = `${assertionResultLabel(item)} · Bugs ${item.suspected_bug_count ?? 0}`
        + (item.execution_error ? ` · ${item.execution_error}` : "");
    cell.append(outcome);
}

function renderOverviewRows(items, emptyMessage) {
    const tbody = document.getElementById("overviewRecentRuns");
    tbody.replaceChildren();
    if (!items.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.className = "empty-cell";
        cell.colSpan = 5;
        cell.textContent = emptyMessage;
        row.append(cell);
        tbody.append(row);
        return;
    }

    for (const item of items) {
        const row = document.createElement("tr");
        const nameCell = document.createElement("td");
        const runName = document.createElement("strong");
        runName.className = "table-run-name";
        runName.textContent = item.run_name;
        const runId = document.createElement("small");
        runId.className = "table-run-id";
        runId.textContent = item.id;
        nameCell.append(runName, runId);
        const platformCell = document.createElement("td");
        platformCell.textContent = item.platform;
        const statusCell = document.createElement("td");
        statusCell.append(createStatusBadge(item.status));
        appendRunOutcome(statusCell, item);
        const dateCell = document.createElement("td");
        dateCell.textContent = formatDate(item.created_at);
        const actionCell = document.createElement("td");
        const openButton = document.createElement("button");
        openButton.type = "button";
        openButton.className = "table-action-button";
        openButton.textContent = "Details";
        openButton.addEventListener("click", () => loadHistoricalRun(item.id));
        actionCell.append(openButton);
        row.append(nameCell, platformCell, statusCell, dateCell, actionCell);
        tbody.append(row);
    }
}

function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
        ? value
        : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function reportUrl(runId, format) {
    return `${API_BASE}/test-runs/${encodeURIComponent(runId)}/reports/${format}`;
}

function buildHistoryTable(items, container, emptyMessage) {
    container.replaceChildren();
    if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "table-empty-state";
        empty.textContent = emptyMessage;
        container.append(empty);
        return;
    }

    const table = document.createElement("table");
    table.className = "data-table";
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const label of ["Run name", "Platform", "Status", "Date", "Actions"]) {
        const th = document.createElement("th");
        th.scope = "col";
        th.textContent = label;
        headerRow.append(th);
    }
    thead.append(headerRow);

    const tbody = document.createElement("tbody");
    for (const item of items) {
        const row = document.createElement("tr");
        const nameCell = document.createElement("td");
        const runName = document.createElement("strong");
        runName.className = "table-run-name";
        runName.textContent = item.run_name;
        const runId = document.createElement("small");
        runId.className = "table-run-id";
        runId.textContent = item.id;
        nameCell.append(runName, runId);

        const platformCell = document.createElement("td");
        platformCell.textContent = item.platform;
        const statusCell = document.createElement("td");
        statusCell.append(createStatusBadge(item.status));
        appendRunOutcome(statusCell, item);
        const dateCell = document.createElement("td");
        dateCell.textContent = formatDate(item.created_at);

        const actionsCell = document.createElement("td");
        actionsCell.className = "table-actions";
        const detailsButton = document.createElement("button");
        detailsButton.type = "button";
        detailsButton.className = "table-action-button";
        detailsButton.textContent = "Details";
        detailsButton.addEventListener("click", () => loadHistoricalRun(item.id));
        const excelLink = document.createElement("a");
        excelLink.href = reportUrl(item.id, "excel");
        excelLink.textContent = "Excel";
        excelLink.setAttribute("aria-label", `Download Excel report for ${item.run_name}`);
        const htmlLink = document.createElement("a");
        htmlLink.href = reportUrl(item.id, "html");
        htmlLink.textContent = "HTML";
        htmlLink.setAttribute("aria-label", `Download HTML report for ${item.run_name}`);
        actionsCell.append(detailsButton, excelLink, htmlLink);

        row.append(nameCell, platformCell, statusCell, dateCell, actionsCell);
        tbody.append(row);
    }
    table.append(thead, tbody);
    container.append(table);
}

function renderHistorySearch() {
    const query = historySearch.value.trim().toLocaleLowerCase();
    const filtered = currentHistoryItems.filter((item) =>
        `${item.run_name} ${item.id}`.toLocaleLowerCase().includes(query)
    );
    buildHistoryTable(
        filtered,
        historyList,
        query ? "No runs on this page match your search." : "No test runs match these filters."
    );
    if (query) {
        historyMessage.textContent = `${filtered.length} matching run${filtered.length === 1 ? "" : "s"} on this page.`;
    } else {
        historyMessage.textContent = currentHistoryItems.length
            ? `${historyTotal} saved test run${historyTotal === 1 ? "" : "s"} found.`
            : "No test runs match these filters.";
    }
}

async function loadExecutionHistory() {
    historyMessage.textContent = "Loading execution history…";
    historyList.replaceChildren();
    const query = new URLSearchParams({
        limit: String(HISTORY_PAGE_SIZE),
        offset: String(historyOffset)
    });
    if (historyStatusSelect.value) query.set("status", historyStatusSelect.value);
    if (historyPlatformSelect.value) query.set("platform", historyPlatformSelect.value);

    try {
        const page = await requestJson(`/test-runs?${query}`);
        currentHistoryItems = page.items;
        historyTotal = page.total;
        renderHistorySearch();
        historyPageLabel.textContent = page.total
            ? `Showing ${historyOffset + 1}–${Math.min(historyOffset + page.items.length, page.total)} of ${page.total}`
            : "No runs";
        historyPreviousBtn.disabled = historyOffset === 0;
        historyNextBtn.disabled = historyOffset + HISTORY_PAGE_SIZE >= historyTotal;
        if (document.getElementById("overviewSection").hidden === false) {
            renderOverviewRows(page.items.slice(0, 5), "No saved test runs yet.");
        }
    } catch (error) {
        currentHistoryItems = [];
        historyMessage.textContent = `Could not load history: ${error.message}`;
        historyPageLabel.textContent = "";
        historyPreviousBtn.disabled = true;
        historyNextBtn.disabled = true;
        renderOverviewRows([], "Saved runs are unavailable while the backend is offline.");
        buildHistoryTable([], historyList, "Execution history could not be loaded.");
    }
}

function extractExecutionLogs(markup) {
    const reportDocument = new DOMParser().parseFromString(markup, "text/html");
    const heading = [...reportDocument.querySelectorAll("h2")]
        .find((element) => element.textContent.trim() === "Execution Logs");
    const list = heading?.nextElementSibling;
    if (!list || list.tagName !== "UL") {
        throw new Error("The saved HTML report does not contain an execution logs section.");
    }
    return [...list.querySelectorAll("li")].map((item) => item.textContent.trim());
}

function appendParagraph(parent, label, value) {
    const paragraph = document.createElement("p");
    if (label) {
        const strong = document.createElement("strong");
        strong.textContent = `${label}: `;
        paragraph.append(strong);
    }
    paragraph.append(document.createTextNode(value));
    parent.append(paragraph);
}

function renderAiAnalysisSection(parent, runId, initialReports) {
    const section = document.createElement("section");
    section.className = "detail-section ai-analysis-section";
    section.id = "runAiAnalysisSection";

    const heading = document.createElement("div");
    heading.className = "section-heading";
    const title = document.createElement("h3");
    title.textContent = "AI Failure Analysis";
    const chip = document.createElement("span");
    chip.className = "ai-chip";
    chip.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.64 5.64l2.12 2.12m8.48 8.48 2.12 2.12m0-12.72-2.12 2.12m-8.48 8.48-2.12 2.12M12 8v8m-4-4h8"></path></svg> Gemini';
    heading.append(title, chip);
    section.append(heading);

    const contentArea = document.createElement("div");
    contentArea.className = "ai-analysis-content";
    section.append(contentArea);

    function displayReports(reports) {
        contentArea.replaceChildren();
        if (!reports || !reports.length) {
            const emptyNotice = document.createElement("p");
            emptyNotice.className = "muted";
            emptyNotice.textContent = "No AI failure analysis has been generated for this run yet.";
            const generateBtn = document.createElement("button");
            generateBtn.className = "primary-button";
            generateBtn.type = "button";
            generateBtn.textContent = "Analyze with AI";
            generateBtn.addEventListener("click", () => runAnalysis(false));
            contentArea.append(emptyNotice, generateBtn);
            return;
        }

        const latest = reports[0];
        const card = document.createElement("article");
        card.className = "run-step ai-analysis-card";

        const cardHeader = document.createElement("div");
        cardHeader.className = "step-heading";
        const reportTitle = document.createElement("strong");
        reportTitle.textContent = latest.title || "AI Failure Analysis";
        const metaSpan = document.createElement("span");
        metaSpan.className = "report-meta";
        metaSpan.textContent = `${formatDate(latest.created_at)} · ${latest.model || "gemini"}`;
        cardHeader.append(reportTitle, metaSpan);
        card.append(cardHeader);

        const formattedBody = formatReport(latest.report_markdown);
        card.append(formattedBody);

        const actionsBar = document.createElement("div");
        actionsBar.className = "report-actions";
        actionsBar.style.marginTop = "12px";

        const reanalyzeBtn = document.createElement("button");
        reanalyzeBtn.className = "secondary-button";
        reanalyzeBtn.type = "button";
        reanalyzeBtn.textContent = "Re-analyze with AI";
        reanalyzeBtn.addEventListener("click", () => runAnalysis(true));

        const workspaceBtn = document.createElement("button");
        workspaceBtn.className = "secondary-button";
        workspaceBtn.type = "button";
        workspaceBtn.textContent = "Open in AI Workspace";
        workspaceBtn.addEventListener("click", () => {
            setActivePage("ai-bug");
            selectSavedAnalysis(latest);
        });

        actionsBar.append(reanalyzeBtn, workspaceBtn);
        card.append(actionsBar);
        contentArea.append(card);
    }

    async function runAnalysis(forceRefresh) {
        const checkedBoxes = section.parentElement.querySelectorAll(".finding-select-box:checked");
        const selectedErrorIds = Array.from(checkedBoxes).map((box) => box.dataset.errorId);

        contentArea.replaceChildren();
        const loadingDiv = document.createElement("div");
        loadingDiv.className = "loading";
        loadingDiv.textContent = "Analyzing test run with AI…";
        contentArea.append(loadingDiv);

        try {
            const result = await requestJson(`/test-runs/${encodeURIComponent(runId)}/analyze`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    force_refresh: forceRefresh,
                    browser_error_ids: selectedErrorIds
                })
            });
            displayReports([result]);
            loadSavedAnalyses().catch(() => {});
        } catch (error) {
            contentArea.replaceChildren();
            const errorMsg = document.createElement("p");
            errorMsg.className = "error-message";
            errorMsg.textContent = `Analysis failed: ${error.message}`;
            const retryBtn = document.createElement("button");
            retryBtn.className = "secondary-button";
            retryBtn.type = "button";
            retryBtn.textContent = "Retry analysis";
            retryBtn.addEventListener("click", () => runAnalysis(forceRefresh));
            contentArea.append(errorMsg, retryBtn);
        }
    }

    displayReports(initialReports);
    parent.append(section);
}

async function loadHistoricalRun(runId) {
    setActivePage("reports");
    historyDetails.replaceChildren();
    const loadingMessage = document.createElement("p");
    loadingMessage.className = "muted";
    loadingMessage.textContent = `Loading run ${runId}…`;
    historyDetails.append(loadingMessage);

    try {
        const [run, steps, browserErrors, aiReports] = await Promise.all([
            requestJson(`/test-runs/${encodeURIComponent(runId)}`),
            requestJson(`/test-runs/${encodeURIComponent(runId)}/steps`),
            requestJson(`/test-runs/${encodeURIComponent(runId)}/browser-errors`).catch(() => []),
            requestJson(`/test-runs/${encodeURIComponent(runId)}/ai-bug-reports`).catch(() => [])
        ]);

        let executionLogs;
        let logLoadError = null;
        try {
            const reportResponse = await fetch(reportUrl(runId, "html"));
            if (!reportResponse.ok) {
                const reportError = await reportResponse.text();
                throw new Error(reportError || `Request failed with status ${reportResponse.status}.`);
            }
            executionLogs = extractExecutionLogs(await reportResponse.text());
        } catch (error) {
            logLoadError = error;
            executionLogs = [];
        }

        historyDetails.replaceChildren();
        const heading = document.createElement("div");
        heading.className = "detail-heading";
        const headingText = document.createElement("div");
        const title = document.createElement("h2");
        title.textContent = run.run_name;
        const runIdText = document.createElement("p");
        runIdText.className = "table-run-id";
        runIdText.textContent = `Run ID · ${run.id}`;
        headingText.append(title, runIdText);
        heading.append(headingText, createStatusBadge(run.status));
        historyDetails.append(heading);

        const metadata = document.createElement("div");
        metadata.className = "detail-metadata";
        for (const [label, value] of [
            ["Platform", run.platform],
            ["Mode", run.mode],
            ["Target", run.target || "None"],
            ["Execution", executionStatusLabel(run)],
            ["Assertions", assertionResultLabel(run)],
            ["Suspected bugs", String(run.suspected_bug_count ?? 0)],
            ["Browser findings", String(browserErrors.length)],
            ["Execution error", run.execution_error || "None"],
            ["Created", formatDate(run.created_at)],
            ["Duration", run.started_at && run.ended_at
                ? `${Math.max(0, new Date(run.ended_at) - new Date(run.started_at))} ms`
                : "Not available"]
        ]) {
            const item = document.createElement("div");
            const labelText = document.createElement("span");
            labelText.textContent = label;
            const valueText = document.createElement("strong");
            valueText.textContent = value;
            item.append(labelText, valueText);
            metadata.append(item);
        }
        historyDetails.append(metadata);

        const reportActions = document.createElement("div");
        reportActions.className = "report-actions";
        for (const [format, label] of [["excel", "Download Excel report"], ["html", "Download HTML report"]]) {
            const link = document.createElement("a");
            link.className = "secondary-button";
            link.href = reportUrl(runId, format);
            link.textContent = label;
            reportActions.append(link);
        }
        historyDetails.append(reportActions);

        const stepsSection = document.createElement("section");
        stepsSection.className = "detail-section";
        const stepsHeading = document.createElement("h3");
        stepsHeading.textContent = `Test steps (${steps.length})`;
        stepsSection.append(stepsHeading);
        if (!steps.length) {
            appendParagraph(stepsSection, "", "No test steps were recorded for this run.");
        }
        for (const step of steps) {
            const card = document.createElement("article");
            card.className = "run-step";
            card.dataset.status = step.status;
            const cardHeading = document.createElement("div");
            cardHeading.className = "step-heading";
            const name = document.createElement("strong");
            name.textContent = `Step ${step.step_number}: ${step.action}`;
            cardHeading.append(name, createStatusBadge(step.status));
            card.append(cardHeading);
            if (step.page_or_screen) appendParagraph(card, "Page", step.page_or_screen);
            if (step.expected_result) appendParagraph(card, "Expected", step.expected_result);
            appendParagraph(card, "Actual", step.actual_result || "No actual result recorded.");
            if (step.duration_ms !== null) appendParagraph(card, "Duration", `${step.duration_ms} ms`);
            if (step.error_details) appendParagraph(card, "Error", step.error_details);
            if (step.screenshot_path) {
                const screenshotUrl = `${API_BASE}/test-runs/${encodeURIComponent(runId)}/steps/${step.step_number}/screenshot`;
                const screenshotLink = document.createElement("a");
                screenshotLink.className = "evidence-link";
                screenshotLink.href = screenshotUrl;
                screenshotLink.target = "_blank";
                screenshotLink.rel = "noopener noreferrer";
                screenshotLink.textContent = "Open screenshot evidence";
                const screenshot = document.createElement("img");
                screenshot.className = "evidence-image";
                screenshot.src = screenshotUrl;
                screenshot.alt = `Screenshot evidence for step ${step.step_number}`;
                screenshot.loading = "lazy";
                screenshot.addEventListener("error", () => {
                    screenshot.remove();
                    screenshotLink.textContent = "Screenshot evidence could not be loaded; open the endpoint to retry.";
                }, { once: true });
                card.append(screenshotLink, screenshot);
            }
            stepsSection.append(card);
        }
        historyDetails.append(stepsSection);

        const browserErrorsSection = document.createElement("section");
        browserErrorsSection.className = "detail-section";
        const browserErrorsHeading = document.createElement("h3");
        browserErrorsHeading.textContent = `Browser findings (${browserErrors.length})`;
        browserErrorsSection.append(browserErrorsHeading);
        if (!browserErrors.length) {
            appendParagraph(browserErrorsSection, "", "No browser errors were detected for this run.");
        } else {
            for (const error of browserErrors) {
                const card = document.createElement("article");
                card.className = "run-step";
                card.dataset.status = "error";
                const cardHeading = document.createElement("div");
                cardHeading.className = "step-heading";
                const name = document.createElement("strong");
                const stepPrefix = error.step_number ? `Step ${error.step_number} · ` : "";
                name.textContent = `${stepPrefix}${error.category}${error.http_status ? ` (${error.http_status})` : ""}`;

                const selectLabel = document.createElement("label");
                selectLabel.className = "finding-select-label";
                const selectBox = document.createElement("input");
                selectBox.type = "checkbox";
                selectBox.className = "finding-select-box";
                selectBox.dataset.errorId = error.id;
                selectLabel.append(selectBox, document.createTextNode("Select for AI"));

                cardHeading.append(name, createStatusBadge("error"), selectLabel);
                card.append(cardHeading);
                appendParagraph(card, "Message", error.message);
                if (error.url) appendParagraph(card, "URL", error.url);
                if (error.resource_type) appendParagraph(card, "Resource type", error.resource_type);
                appendParagraph(card, "Timestamp", formatDate(error.timestamp));
                browserErrorsSection.append(card);
            }
        }
        historyDetails.append(browserErrorsSection);

        renderAiAnalysisSection(historyDetails, runId, aiReports);

        const logsSection = document.createElement("section");
        logsSection.className = "detail-section";
        const logsHeading = document.createElement("h3");
        logsHeading.textContent = `Execution logs${logLoadError ? "" : ` (${executionLogs.length})`}`;
        logsSection.append(logsHeading);
        if (logLoadError) {
            appendParagraph(logsSection, "", `Could not load execution logs: ${logLoadError.message}`);
        } else if (!executionLogs.length) {
            appendParagraph(logsSection, "", "No execution logs were recorded for this run.");
        } else {
            const logList = document.createElement("ul");
            logList.className = "execution-log-list";
            for (const entry of executionLogs) {
                const logItem = document.createElement("li");
                logItem.textContent = entry;
                logList.append(logItem);
            }
            logsSection.append(logList);
        }
        historyDetails.append(logsSection);
        historyMessage.textContent = `Opened run ${run.id}.`;
    } catch (error) {
        historyDetails.replaceChildren();
        const message = document.createElement("p");
        message.className = "error-message";
        message.textContent = `Could not open run: ${error.message}`;
        historyDetails.append(message);
        historyMessage.textContent = `Could not open run: ${error.message}`;
    }
}

async function pollTestRun(runId) {
    while (true) {
        const [run, steps] = await Promise.all([
            requestJson(`/test-runs/${encodeURIComponent(runId)}`),
            requestJson(`/test-runs/${encodeURIComponent(runId)}/steps`)
        ]);
        renderTestRun(run, steps);
        if (["completed", "failed", "cancelled"].includes(run.status)) return;
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
}

function renderTestRun(run, steps) {
    runStatus.textContent = (
        `Run ${run.id} — ${executionStatusLabel(run)} — ${assertionResultLabel(run)}`
        + (run.execution_error ? ` (${run.execution_error})` : "")
        + (run.error_details ? `: ${run.error_details}` : "")
    );
    runSteps.replaceChildren();
    for (const step of steps) {
        const card = document.createElement("article");
        card.className = "run-step";
        card.dataset.status = step.status;
        const heading = document.createElement("div");
        heading.className = "step-heading";
        const title = document.createElement("strong");
        title.textContent = `Step ${step.step_number}: ${step.action}`;
        heading.append(title, createStatusBadge(step.status));
        const actual = document.createElement("p");
        actual.textContent = step.actual_result || "No actual result recorded.";
        card.append(heading, actual);
        if (step.error_details) appendParagraph(card, "Error", step.error_details);
        if (step.screenshot_path) appendParagraph(card, "Screenshot recorded", step.screenshot_path);
        runSteps.append(card);
    }
}

async function checkBackendConnection() {
    const status = document.getElementById("backendStatus");
    const statusText = document.getElementById("backendStatusText");
    status.dataset.status = "checking";
    statusText.textContent = "Checking backend";
    try {
        const response = await fetch(`${API_BASE}/`);
        if (!response.ok) throw new Error(`Backend returned ${response.status}.`);
        status.dataset.status = "connected";
        statusText.textContent = "Backend connected";
    } catch (error) {
        status.dataset.status = "disconnected";
        statusText.textContent = "Backend unavailable";
    }
}

async function loadDashboardSummary() {
    const totalValue = document.getElementById("totalRunsStat");
    const passedValue = document.getElementById("passedRunsStat");
    const failedValue = document.getElementById("failedRunsStat");
    const bugValue = document.getElementById("bugsDetectedStat");
    const summaryMessage = document.getElementById("overviewStatusMessage");
    try {
        const [metrics, recentRuns] = await Promise.all([
            requestJson("/dashboard/metrics"),
            requestJson("/test-runs?limit=5&offset=0")
        ]);
        totalValue.textContent = String(metrics.total_runs);
        passedValue.textContent = String(metrics.completed_runs);
        failedValue.textContent = String(metrics.failed_runs);
        document.getElementById("cancelledRunsStat").textContent = String(metrics.cancelled_runs);
        document.getElementById("assertionsPassedStat").textContent = String(metrics.assertions_passed);
        document.getElementById("assertionsFailedStat").textContent = String(metrics.assertions_failed);
        bugValue.textContent = String(metrics.suspected_bugs);
        summaryMessage.textContent = "";
        renderOverviewRows(recentRuns.items, "No saved test runs yet.");
    } catch (error) {
        totalValue.textContent = "—";
        passedValue.textContent = "—";
        failedValue.textContent = "—";
        document.getElementById("cancelledRunsStat").textContent = "—";
        document.getElementById("assertionsPassedStat").textContent = "—";
        document.getElementById("assertionsFailedStat").textContent = "—";
        bugValue.textContent = "—";
        summaryMessage.textContent = `Could not calculate dashboard metrics: ${error.message}`;
        renderOverviewRows([], `Could not load saved runs: ${error.message}`);
    }
}

historySearch.addEventListener("input", renderHistorySearch);
document.getElementById("refreshHistoryBtn").addEventListener("click", () => {
    historyOffset = 0;
    loadExecutionHistory();
});
historyStatusSelect.addEventListener("change", () => {
    historyOffset = 0;
    loadExecutionHistory();
});
historyPlatformSelect.addEventListener("change", () => {
    historyOffset = 0;
    loadExecutionHistory();
});
historyPreviousBtn.addEventListener("click", () => {
    historyOffset = Math.max(0, historyOffset - HISTORY_PAGE_SIZE);
    loadExecutionHistory();
});
historyNextBtn.addEventListener("click", () => {
    if (historyOffset + HISTORY_PAGE_SIZE < historyTotal) {
        historyOffset += HISTORY_PAGE_SIZE;
        loadExecutionHistory();
    }
});
const refreshDashboardBtn = document.getElementById("refreshDashboardBtn");
if (refreshDashboardBtn) {
    refreshDashboardBtn.addEventListener("click", async () => {
        await Promise.all([checkBackendConnection(), loadDashboardSummary(), loadExecutionHistory()]);
    });
}

bootstrapSession()
    .then(() => Promise.all([checkBackendConnection(), loadDashboardSummary(), loadExecutionHistory(), loadSavedAnalyses()]))
    .catch((error) => {
        console.error(error);
        document.getElementById("backendStatusText").textContent = "Secure session unavailable";
    });
