"use strict";

// Syndicate monitor -- vanilla JS, no build step. Every dynamic value is
// rendered via textContent / DOM APIs, never innerHTML, per the XSS rule:
// tickets, outputs, and state contain arbitrary strings.

const timelineEl = document.getElementById("timeline");
const runSelectorEl = document.getElementById("run-selector");
const statusPillEl = document.getElementById("status-pill");
const runIdLabelEl = document.getElementById("run-id-label");
const promptFormEl = document.getElementById("prompt-form");
const promptInputEl = document.getElementById("prompt-input");
const promptNoticeEl = document.getElementById("prompt-notice");
const submitBtnEl = document.getElementById("submit-btn");
const rawLogEl = document.getElementById("raw-log");
const tabStateEl = document.getElementById("tab-state");
const tabLedgerEl = document.getElementById("tab-ledger");

let currentRunId = null;
let lastSeq = 0;
let eventSource = null;
let autoScrollPaused = false;
let activeRunIdGlobal = null;
const RAW_LOG_MAX_LINES = 500;

// ---------------------------------------------------------------------------
// DOM helpers -- textContent/DOM APIs only, never innerHTML with runtime data
// ---------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// ---------------------------------------------------------------------------
// Status pill
// ---------------------------------------------------------------------------

function setStatusPill(status) {
  statusPillEl.textContent = status;
  statusPillEl.className = "pill pill-" + status;
}

// ---------------------------------------------------------------------------
// Prompt bar (disabled while any run is active, mirroring the API's 409)
// ---------------------------------------------------------------------------

function setPromptActive(activeRunId) {
  activeRunIdGlobal = activeRunId;
  const isActive = !!activeRunId;
  submitBtnEl.disabled = isActive;
  promptInputEl.disabled = isActive;
  if (isActive) {
    promptNoticeEl.textContent = "A run is currently active (" + activeRunId + ") -- submission disabled until it finishes.";
    promptNoticeEl.hidden = false;
  } else {
    promptNoticeEl.hidden = true;
  }
}

async function pollHealth() {
  try {
    const resp = await fetch("/health");
    const data = await resp.json();
    setPromptActive(data.active_run_id);
  } catch (err) {
    // Server unreachable; leave prompt state as-is rather than guessing.
  }
}

// ---------------------------------------------------------------------------
// Timeline rendering
// ---------------------------------------------------------------------------

function formatTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch (err) {
    return iso;
  }
}

function summarizePayload(envelope) {
  const payload = envelope.payload || {};
  if (envelope.kind === "node_update") {
    const keys = Object.keys(payload);
    return keys.length ? keys.join(", ") : "(no state change)";
  }
  if (envelope.kind === "attempt") {
    return "strike " + payload.strike + ": " + (payload.fast_check_summary || "");
  }
  if (envelope.kind === "escalation") {
    return JSON.stringify(payload);
  }
  if (envelope.kind === "run_started") {
    return payload.raw_request || "";
  }
  if (envelope.kind === "run_completed" || envelope.kind === "run_failed") {
    return JSON.stringify(payload);
  }
  return JSON.stringify(payload);
}

function renderCard(envelope) {
  const card = el("div", "card card-" + envelope.kind);
  if (envelope.kind === "attempt" && envelope.payload && envelope.payload.strike) {
    card.classList.add("card-attempt-" + envelope.payload.strike);
  }

  const header = el("div", "card-header");
  const kindLabel = envelope.kind + (envelope.node ? " · " + envelope.node : "");
  header.appendChild(el("span", "card-kind", kindLabel));
  header.appendChild(el("span", "card-ts", formatTime(envelope.ts)));
  card.appendChild(header);

  card.appendChild(el("div", "card-body", summarizePayload(envelope)));

  timelineEl.appendChild(card);
  if (!autoScrollPaused) {
    timelineEl.scrollTop = timelineEl.scrollHeight;
  }
}

function appendRawLine(envelope) {
  rawLogEl.textContent += JSON.stringify(envelope) + "\n";
  const lines = rawLogEl.textContent.split("\n");
  if (lines.length > RAW_LOG_MAX_LINES) {
    rawLogEl.textContent = lines.slice(lines.length - RAW_LOG_MAX_LINES).join("\n");
  }
}

timelineEl.addEventListener("mouseenter", () => { autoScrollPaused = true; });
timelineEl.addEventListener("mouseleave", () => { autoScrollPaused = false; });

// ---------------------------------------------------------------------------
// State / Ledger tabs
// ---------------------------------------------------------------------------

function renderKv(container, key, value) {
  const row = el("div", "kv-row");
  row.appendChild(el("span", "kv-key", key));
  const valText = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  row.appendChild(el("span", "kv-val", valText === undefined ? "" : valText));
  container.appendChild(row);
}

async function refreshState(runId) {
  if (!runId) return;
  try {
    const resp = await fetch("/runs/" + encodeURIComponent(runId) + "/state");
    if (!resp.ok) return;
    const state = await resp.json();

    clearChildren(tabStateEl);
    const skip = new Set(["decision_ledger"]);
    for (const key of Object.keys(state)) {
      if (skip.has(key)) continue;
      renderKv(tabStateEl, key, state[key]);
    }
    if (Object.keys(state).length === 0) {
      tabStateEl.appendChild(el("div", "empty-note", "No state yet."));
    }

    clearChildren(tabLedgerEl);
    const ledger = state.decision_ledger || [];
    if (ledger.length === 0) {
      tabLedgerEl.appendChild(el("div", "empty-note", "No decision_ledger entries yet."));
    }
    ledger.forEach((entry, idx) => {
      const card = el("div", "ledger-card");
      card.appendChild(el("div", "ledger-count", "Entry " + (idx + 1) + " · attempt_count=" + entry.attempt_count));
      card.appendChild(el("div", "", entry.summary));
      tabLedgerEl.appendChild(card);
    });
  } catch (err) {
    // Transient fetch failure; next event will trigger another refresh.
  }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ---------------------------------------------------------------------------
// SSE stream with manual reconnect (after=lastSeq), since native EventSource
// retry would replay from the URL's original `after` value.
// ---------------------------------------------------------------------------

function attachStream(runId, afterSeq) {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  currentRunId = runId;
  lastSeq = afterSeq;
  runIdLabelEl.textContent = runId;

  const url = "/runs/" + encodeURIComponent(runId) + "/stream?after=" + lastSeq;
  eventSource = new EventSource(url);

  eventSource.onmessage = (event) => {
    let envelope;
    try {
      envelope = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (typeof envelope.seq === "number") {
      lastSeq = envelope.seq;
    }
    renderCard(envelope);
    appendRawLine(envelope);

    if (envelope.kind === "run_started") setStatusPill("running");
    if (envelope.kind === "run_completed") setStatusPill("completed");
    if (envelope.kind === "run_failed") setStatusPill("failed");
    if (envelope.kind === "escalation" && envelope.payload && envelope.payload.ticket_status === "escalated") {
      setStatusPill("escalated");
    }

    refreshState(currentRunId);

    if (envelope.kind === "run_completed" || envelope.kind === "run_failed") {
      pollHealth();
      refreshRunList();
    }
  };

  eventSource.onerror = () => {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    // Manual reconnect with the last-seen seq as the resume cursor -- no
    // gaps, no duplicates (server replays backlog strictly seq > after).
    setTimeout(() => {
      if (currentRunId === runId) attachStream(runId, lastSeq);
    }, 2000);
  };
}

// ---------------------------------------------------------------------------
// Run selector / history
// ---------------------------------------------------------------------------

async function refreshRunList() {
  try {
    const resp = await fetch("/runs");
    const runs = await resp.json();
    const previousValue = runSelectorEl.value;
    clearChildren(runSelectorEl);
    runs.forEach((run) => {
      const opt = el("option", "", run.run_id + " (" + run.status + ")");
      opt.value = run.run_id;
      runSelectorEl.appendChild(opt);
    });
    if (previousValue && runs.some((r) => r.run_id === previousValue)) {
      runSelectorEl.value = previousValue;
    } else if (currentRunId) {
      runSelectorEl.value = currentRunId;
    }
  } catch (err) {
    // Transient fetch failure; leave the selector as-is.
  }
}

runSelectorEl.addEventListener("change", () => {
  const runId = runSelectorEl.value;
  if (!runId || runId === currentRunId) return;
  clearChildren(timelineEl);
  rawLogEl.textContent = "";
  setStatusPill("idle");
  attachStream(runId, 0);
});

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------

promptFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const rawRequest = promptInputEl.value.trim();
  if (!rawRequest) return;

  try {
    const resp = await fetch("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw_request: rawRequest }),
    });
    const data = await resp.json();

    if (resp.status === 409) {
      setPromptActive(data.run_id);
      return;
    }
    if (!resp.ok) {
      return;
    }

    promptInputEl.value = "";
    clearChildren(timelineEl);
    rawLogEl.textContent = "";
    setStatusPill("queued");
    setPromptActive(data.run_id);
    attachStream(data.run_id, 0);
    refreshRunList();
  } catch (err) {
    // Network error; the notice area is left as-is, user can retry.
  }
});

// ---------------------------------------------------------------------------
// Resizable divider (pointer-drag)
// ---------------------------------------------------------------------------

(function setupDivider() {
  const divider = document.getElementById("divider");
  const paneLeft = document.getElementById("pane-left");
  const mainEl = document.getElementById("app-main");
  let dragging = false;

  divider.addEventListener("pointerdown", (event) => {
    dragging = true;
    divider.classList.add("dragging");
    divider.setPointerCapture(event.pointerId);
  });

  divider.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = mainEl.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const clamped = Math.min(Math.max(ratio, 0.2), 0.8);
    paneLeft.style.flex = clamped + " 1 0";
  });

  function stopDrag() {
    dragging = false;
    divider.classList.remove("dragging");
  }
  divider.addEventListener("pointerup", stopDrag);
  divider.addEventListener("pointercancel", stopDrag);
})();

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  await refreshRunList();
  await pollHealth();
  if (runSelectorEl.value) {
    attachStream(runSelectorEl.value, 0);
    refreshState(runSelectorEl.value);
  }
  setInterval(pollHealth, 4000);
  setInterval(refreshRunList, 8000);
}

init();
