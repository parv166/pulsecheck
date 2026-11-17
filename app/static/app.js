const API = "/api/targets";
const state = { targets: [], historyTarget: null, csrfToken: null };

const $ = (id) => document.getElementById(id);

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[char]);
}

function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.add("visible");
    window.setTimeout(() => toast.classList.remove("visible"), 3000);
}

async function requestJSON(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});

    if (
        state.csrfToken &&
        !["GET", "HEAD", "OPTIONS"].includes(method)
    ) {
        headers.set("X-CSRF-Token", state.csrfToken);
    }

    const response = await fetch(url, {
        ...options,
        method,
        headers,
        credentials: "same-origin"
    });

    const data = await response.json().catch(() => ({}));

    if (response.status === 401) {
        window.location.replace("/login");
        throw new Error("Your session has expired. Please sign in again.");
    }

    if (!response.ok) {
        throw new Error(data.detail || `Request failed (${response.status})`);
    }

    return data;
}

function formatLatency(value) {
    return typeof value === "number" ? `${value.toFixed(1)} ms` : "—";
}

function formatDate(value) {
    if (!value) return "Not checked";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}


function renderHistoryChart(records) {
    const host = $("history-chart");

    const points = records
        .filter(record => typeof record.latency_ms === "number")
        .slice()
        .reverse();

    const statusSegments = records.map(record => {
        const status = (record.status || "PENDING").toLowerCase();
        const safeStatus = ["up", "down"].includes(status) ? status : "pending";

        return `
            <span
                class="availability-segment ${safeStatus}"
                title="${escapeHTML(record.status)} · ${escapeHTML(formatDate(record.checked_at))}"
            ></span>
        `;
    }).join("");

    let chart = "";

    if (points.length < 2) {
        chart = '<div class="chart-empty">At least two monitoring results are needed to display a response-time trend.</div>';
    } else {
        const width = 900;
        const height = 250;
        const left = 65;
        const right = 20;
        const top = 20;
        const bottom = 40;

        const values = points.map(point => point.latency_ms);
        const rawMin = Math.min(...values);
        const rawMax = Math.max(...values);
        const range = Math.max(rawMax - rawMin, rawMax * 0.2, 1);

        const min = Math.max(0, rawMin - range * 0.15);
        const max = rawMax + range * 0.15;

        const plotWidth = width - left - right;
        const plotHeight = height - top - bottom;

        const x = index =>
            left + (index / (points.length - 1)) * plotWidth;

        const y = value =>
            top + ((max - value) / (max - min)) * plotHeight;

        const coordinates = points.map((point, index) =>
            `${x(index).toFixed(1)},${y(point.latency_ms).toFixed(1)}`
        );

        const grid = [0, 0.5, 1].map(fraction => {
            const position = top + fraction * plotHeight;
            const value = max - fraction * (max - min);

            return `
                <line class="chart-grid-line"
                    x1="${left}" y1="${position}"
                    x2="${width - right}" y2="${position}" />
                <text class="chart-axis-label"
                    x="${left - 10}" y="${position + 4}"
                    text-anchor="end">${value.toFixed(0)} ms</text>
            `;
        }).join("");

        const circles = points.map((point, index) => `
            <circle class="chart-point"
                cx="${x(index)}" cy="${y(point.latency_ms)}" r="4">
                <title>${point.latency_ms.toFixed(1)} ms · ${escapeHTML(formatDate(point.checked_at))}</title>
            </circle>
        `).join("");

        const firstTime = new Date(points[0].checked_at).toLocaleTimeString();
        const lastTime = new Date(points[points.length - 1].checked_at).toLocaleTimeString();

        chart = `
            <svg class="response-chart"
                 viewBox="0 0 ${width} ${height}"
                 role="img"
                 aria-label="Response time history">
                ${grid}
                <polyline class="chart-line"
                    points="${coordinates.join(" ")}" />
                ${circles}
                <text class="chart-axis-label"
                    x="${left}" y="${height - 10}">${escapeHTML(firstTime)}</text>
                <text class="chart-axis-label"
                    x="${width - right}" y="${height - 10}"
                    text-anchor="end">${escapeHTML(lastTime)}</text>
            </svg>
        `;
    }

    host.innerHTML = `
        <div class="chart-heading">
            <span>Response time trend</span>
            <span>${points.length} recorded checks</span>
        </div>
        ${chart}
        <div class="availability-caption">
            <span>Recent availability checks</span>
            <span>Oldest → Newest</span>
        </div>
        <div class="availability-strip">
            ${statusSegments}
        </div>
    `;
}

function getStatus(check) {
    return check?.status || "PENDING";
}

function renderStats() {
    const total = state.targets.length;
    const up = state.targets.filter(item => getStatus(item.last_check) === "UP").length;
    const down = state.targets.filter(item => getStatus(item.last_check) === "DOWN").length;

    const latencies = state.targets
        .map(item => item.last_check?.latency_ms)
        .filter(value => typeof value === "number");

    const average = latencies.length
        ? latencies.reduce((sum, value) => sum + value, 0) / latencies.length
        : null;

    $("stat-total").textContent = total;
    $("stat-up").textContent = up;
    $("stat-down").textContent = down;
    $("stat-latency").textContent = average === null ? "—" : formatLatency(average);
    $("monitor-count").textContent = `${total} monitor${total === 1 ? "" : "s"}`;
}

function renderTargets() {
    const body = $("targets-body");
    const empty = $("empty-state");
    const error = $("error-message");

    error.hidden = true;
    body.innerHTML = "";

    if (!state.targets.length) {
        empty.hidden = false;
        return;
    }

    empty.hidden = true;

    body.innerHTML = state.targets.map(({ target, last_check }) => {
        const status = getStatus(last_check);
        const statusClass = status.toLowerCase();
        const statusCode = last_check?.status_code ?? "—";

        return `
            <tr>
                <td>
                    <span class="app-name">${escapeHTML(target.name)}</span>
                    <span class="app-url">${escapeHTML(target.url)}</span>
                </td>
                <td><span class="status-pill status-${statusClass}">● ${escapeHTML(status)}</span></td>
                <td>${escapeHTML(statusCode)}</td>
                <td>${formatLatency(last_check?.latency_ms)}</td>
                <td>${escapeHTML(formatDate(last_check?.checked_at))}</td>
                <td>
                    <div class="action-group">
                        <button class="action-button" data-action="check" data-id="${escapeHTML(target.id)}">Check</button>
                        <button class="action-button" data-action="history" data-id="${escapeHTML(target.id)}">History</button>
                        <button class="action-button danger" data-action="delete" data-id="${escapeHTML(target.id)}">Delete</button>
                    </div>
                </td>
            </tr>
        `;
    }).join("");
}

async function refreshVisibleHistory() {
    const id = state.historyTarget;

    if (!id || $("history-panel").hidden) return;

    const item = state.targets.find(entry => entry.target.id === id);

    if (!item) {
        $("history-panel").hidden = true;
        state.historyTarget = null;
        return;
    }

    const content = $("history-content");

    try {
        const data = await requestJSON(
            `${API}/${encodeURIComponent(id)}/history?limit=20`
        );

        if (state.historyTarget !== id || $("history-panel").hidden) return;

        const records = data.history || [];
        renderHistoryChart(records);

        $("history-title").textContent = `${item.target.name} — History`;

        if (!records.length) {
            content.innerHTML =
                '<div class="history-list history-muted">No history available yet.</div>';
            return;
        }

        content.innerHTML = `
            <div class="history-list">
                ${records.map(record => `
                    <div class="history-row">
                        <span>${escapeHTML(formatDate(record.checked_at))}</span>
                        <span class="status-pill status-${escapeHTML((record.status || "PENDING").toLowerCase())}">
                            ${escapeHTML(record.status)}
                        </span>
                        <span class="history-muted">HTTP ${escapeHTML(record.status_code ?? "—")}</span>
                        <span>${formatLatency(record.latency_ms)}</span>
                    </div>
                `).join("")}
            </div>
        `;
    } catch (error) {
        content.innerHTML =
            `<div class="error-message">${escapeHTML(error.message)}</div>`;
    }
}

async function loadTargets() {
    try {
        const data = await requestJSON(API);
        state.targets = data.targets || [];

        renderStats();
        renderTargets();

        $("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
        await refreshVisibleHistory();
        return true;
    } catch (error) {
        $("error-message").textContent = `Could not load monitors: ${error.message}`;
        $("error-message").hidden = false;
        return false;
    }
}

async function checkEngine() {
    try {
        await requestJSON("/health");
        $("engine-status").textContent = "Operational";
    } catch {
        $("engine-status").textContent = "Unavailable";
    }
}

async function addTarget(event) {
    event.preventDefault();

    const button = $("submit-target");
    button.disabled = true;
    button.textContent = "Adding...";

    try {
        await requestJSON(API, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: $("target-name").value.trim(),
                url: $("target-url").value.trim()
            })
        });

        $("add-dialog").close();
        $("add-form").reset();
        showToast("Monitor added successfully.");
        await loadTargets();
    } catch (error) {
        showToast(error.message);
    } finally {
        button.disabled = false;
        button.textContent = "Add monitor";
    }
}

async function manualCheck(id, button) {
    button.disabled = true;
    button.textContent = "Checking...";

    try {
        const result = await requestJSON(`${API}/${encodeURIComponent(id)}/check`, {
            method: "POST"
        });
        showToast(`Check completed: ${result.status}`);
        await loadTargets();
    } catch (error) {
        showToast(error.message);
    } finally {
        button.disabled = false;
        button.textContent = "Check";
    }
}

async function deleteTarget(id) {
    const item = state.targets.find(entry => entry.target.id === id);
    if (!item) return;

    if (!window.confirm(`Delete monitor "${item.target.name}" and its stored history?`)) {
        return;
    }

    try {
        await requestJSON(`${API}/${encodeURIComponent(id)}`, {
            method: "DELETE"
        });

        $("history-panel").hidden = true;
        showToast("Monitor deleted.");
        await loadTargets();
    } catch (error) {
        showToast(error.message);
    }
}

async function showHistory(id) {
    const item = state.targets.find(entry => entry.target.id === id);
    if (!item) return;

    const panel = $("history-panel");
    const content = $("history-content");

    state.historyTarget = id;
    $("history-title").textContent = `${item.target.name} — History`;
    panel.hidden = false;
    content.innerHTML = '<div class="history-list history-muted">Loading history...</div>';

    try {
        const data = await requestJSON(`${API}/${encodeURIComponent(id)}/history?limit=20`);
        const records = data.history || [];
        renderHistoryChart(records);

        if (!records.length) {
            content.innerHTML = '<div class="history-list history-muted">No history available yet.</div>';
        } else {
            content.innerHTML = `
                <div class="history-list">
                    ${records.map(record => `
                        <div class="history-row">
                            <span>${escapeHTML(formatDate(record.checked_at))}</span>
                            <span class="status-pill status-${escapeHTML((record.status || "PENDING").toLowerCase())}">
                                ${escapeHTML(record.status)}
                            </span>
                            <span class="history-muted">HTTP ${escapeHTML(record.status_code ?? "—")}</span>
                            <span>${formatLatency(record.latency_ms)}</span>
                        </div>
                    `).join("")}
                </div>
            `;
        }

        panel.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
        content.innerHTML = `<div class="error-message">${escapeHTML(error.message)}</div>`;
    }
}

document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;

    const id = button.dataset.id;
    const action = button.dataset.action;

    if (action === "check") await manualCheck(id, button);
    if (action === "history") await showHistory(id);
    if (action === "delete") await deleteTarget(id);
});

$("open-add-dialog").addEventListener("click", () => $("add-dialog").showModal());
$("empty-add-button").addEventListener("click", () => $("add-dialog").showModal());
$("close-dialog").addEventListener("click", () => $("add-dialog").close());
$("cancel-dialog").addEventListener("click", () => $("add-dialog").close());
$("add-form").addEventListener("submit", addTarget);
$("refresh-button").addEventListener("click", loadTargets);
$("close-history").addEventListener("click", () => $("history-panel").hidden = true);

$("logout-button").addEventListener("click", async () => {
    const button = $("logout-button");
    button.disabled = true;

    try {
        await requestJSON("/api/auth/logout", {
            method: "POST"
        });
        window.location.replace("/login");
    } catch (error) {
        showToast(error.message);
        button.disabled = false;
    }
});

async function initialize() {
    try {
        const session = await requestJSON("/api/auth/session");
        state.csrfToken = session.csrf_token;

        await Promise.all([loadTargets(), checkEngine()]);

        try {
            const version = await requestJSON("/version");
            $("app-version").textContent = `v${version.version}`;
        } catch {
            $("app-version").textContent = "";
        }

        window.setInterval(loadTargets, 10000);
    } catch (error) {
        console.error("Dashboard initialization failed:", error);
    }
}

initialize();
