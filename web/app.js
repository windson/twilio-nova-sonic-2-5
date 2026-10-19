"use strict";

// Dependency-light vanilla-JS dashboard for the AR Collections Nova Sonic 2.5
// agent. No build step, no npm. Talks to the FastAPI REST API:
//   GET  /api/dues          -> list due records
//   POST /api/calls         -> initiate an outbound call for an account
//   GET  /api/calls         -> list call analytics summaries
//   GET  /api/calls/{id}    -> full CallAnalytics (turns, transcripts, log)
//   GET  /api/config        -> non-secret config summary

const POLL_INTERVAL_MS = 3000;

const state = {
  selectedCallId: null,
  pollTimer: null,
};

// --- helpers ---------------------------------------------------------------

function $(id) {
  return document.getElementById(id);
}

function fmtMs(value) {
  if (value === null || value === undefined) return "\u2013";
  const n = Number(value);
  if (Number.isNaN(n)) return "\u2013";
  if (n >= 1000) return (n / 1000).toFixed(2) + " s";
  return Math.round(n) + " ms";
}

function fmtMoney(amount, currency) {
  if (amount === null || amount === undefined) return "\u2013";
  const cur = currency || "USD";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: cur,
    }).format(Number(amount));
  } catch (e) {
    return `${Number(amount).toFixed(2)} ${cur}`;
  }
}

function showToast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  el.classList.toggle("toast-error", !!isError);
  setTimeout(() => {
    el.hidden = true;
  }, 5000);
}

async function apiGet(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail && body.detail.message ? body.detail.message : JSON.stringify(body.detail || body);
    } catch (e) {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail && data.detail.message ? data.detail.message : JSON.stringify(data.detail || data);
    } catch (e) {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

// --- due records -----------------------------------------------------------

async function loadDues() {
  const body = $("dues-body");
  try {
    const data = await apiGet("/api/dues");
    const dues = data.dues || [];
    if (dues.length === 0) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No due records.</td></tr>';
      return;
    }
    body.innerHTML = "";
    for (const due of dues) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${due.account_id || ""}</td>
        <td>${due.debtor_name || ""}</td>
        <td>${fmtMoney(due.amount_due, due.currency)}</td>
        <td>${due.due_date || ""}</td>
        <td><span class="status status-${due.status || "open"}">${due.status || ""}</span></td>
        <td></td>`;
      const btn = document.createElement("button");
      btn.textContent = "Call now";
      btn.className = "call-btn";
      btn.addEventListener("click", () => placeCall(due.account_id, btn));
      tr.lastElementChild.appendChild(btn);
      body.appendChild(tr);
    }
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="empty">Failed to load dues: ${e.message}</td></tr>`;
  }
}

async function placeCall(accountId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Calling\u2026";
  }
  try {
    const result = await apiPost("/api/calls", { account_id: accountId });
    showToast(`Outbound call initiated for ${accountId}: ${result.call_sid}`);
    await loadCalls();
    // Select the new call and poll live so metrics populate.
    if (result.call_sid) {
      selectCall(result.call_sid, true);
    }
  } catch (e) {
    showToast(`Could not place call: ${e.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Call now";
    }
  }
}

// --- calls list ------------------------------------------------------------

async function loadCalls() {
  const body = $("calls-body");
  try {
    const data = await apiGet("/api/calls");
    const calls = data.calls || [];
    if (calls.length === 0) {
      body.innerHTML = '<tr><td colspan="4" class="empty">No calls yet.</td></tr>';
      return;
    }
    body.innerHTML = "";
    for (const call of calls) {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      if (call.call_sid === state.selectedCallId) tr.classList.add("selected");
      tr.innerHTML = `
        <td class="mono">${call.call_sid || ""}</td>
        <td>${call.account_id || ""}</td>
        <td>${call.turn_count != null ? call.turn_count : "\u2013"}</td>
        <td>${fmtMs(call.total_e2e_ms)}</td>`;
      tr.addEventListener("click", () => selectCall(call.call_sid, false));
      body.appendChild(tr);
    }
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4" class="empty">Failed to load calls: ${e.message}</td></tr>`;
  }
}

// --- call detail + live polling --------------------------------------------

function selectCall(callId, live) {
  state.selectedCallId = callId;
  $("detail-panel").hidden = false;
  $("detail-call-id").textContent = callId;
  stopPolling();
  renderDetail();
  if (live) {
    startPolling();
  }
}

function startPolling() {
  $("live-badge").hidden = false;
  state.pollTimer = setInterval(renderDetail, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  $("live-badge").hidden = true;
}

async function renderDetail() {
  if (!state.selectedCallId) return;
  let record;
  try {
    record = await apiGet(`/api/calls/${encodeURIComponent(state.selectedCallId)}`);
  } catch (e) {
    // Call may not be persisted yet (still active): keep polling quietly.
    return;
  }

  $("m-total").textContent = fmtMs(record.total_e2e_ms);
  $("m-nova").textContent = fmtMs(record.nova_total_ms);
  $("m-nonnova").textContent = fmtMs(record.non_nova_ms);
  $("m-twilio").textContent = fmtMs(record.twilio_latency_ms);
  $("m-ttft").textContent = fmtMs(record.ttft_ms);
  $("m-turns").textContent = record.turn_count != null ? record.turn_count : "\u2013";
  $("m-barge").textContent = record.barge_in_count != null ? record.barge_in_count : "\u2013";

  renderTurns(record.turns || [], record.transcripts || []);
  $("log-pre").textContent = JSON.stringify(record.events_log || [], null, 2);

  // If the call has finished (total e2e known), stop the live polling.
  if (record.total_e2e_ms != null) {
    stopPolling();
    loadCalls();
  }
}

function transcriptForTurn(turn, turnIndex, turns, transcripts) {
  // Align final assistant transcripts to a turn by timestamp rather than by
  // position. Each turn has a start time (start_ms); the turn's window runs
  // from its start to the next turn's start (or the end of the call). We join
  // every final assistant transcript whose t_ms falls inside that window, so
  // barge-ins and tool-only turns no longer shift transcripts off by one.
  const start = turn.start_ms;
  if (start === null || start === undefined) {
    // Fall back to positional alignment when timing is unavailable.
    const assistant = transcripts.filter((t) => t.role === "assistant" && t.is_final);
    return assistant.length > turnIndex ? assistant[turnIndex].text || "" : "";
  }
  const next = turns[turnIndex + 1];
  const end =
    next && next.start_ms !== null && next.start_ms !== undefined
      ? next.start_ms
      : Infinity;
  const texts = transcripts
    .filter(
      (t) =>
        t.role === "assistant" &&
        t.is_final &&
        t.t_ms !== null &&
        t.t_ms !== undefined &&
        t.t_ms >= start &&
        t.t_ms < end
    )
    .map((t) => t.text || "")
    .filter(Boolean);
  return texts.join(" ");
}

function renderTurns(turns, transcripts) {
  const body = $("turns-body");
  if (!turns.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">No turns recorded.</td></tr>';
    return;
  }
  body.innerHTML = "";
  turns.forEach((turn, i) => {
    const tr = document.createElement("tr");
    const text = transcriptForTurn(turn, i, turns, transcripts);
    tr.innerHTML = `
      <td>${turn.index != null ? turn.index + 1 : i + 1}</td>
      <td>${fmtMs(turn.duration_ms)}</td>
      <td>${fmtMs(turn.turn_latency_ms)}</td>
      <td>${fmtMs(turn.ttft_ms)}</td>
      <td class="transcript">${(text || "").replace(/</g, "&lt;")}</td>`;
    body.appendChild(tr);
  });
}

// --- config footer ---------------------------------------------------------

async function loadConfig() {
  try {
    const cfg = await apiGet("/api/config");
    const parts = [
      `model: ${cfg.nova_model_id}`,
      `region: ${cfg.aws_region}`,
      `twilio: ${cfg.twilio_configured ? "configured" : "NOT configured"}`,
      `public_base_url: ${cfg.public_base_url_set ? "set" : "missing"}`,
    ];
    $("config-summary").textContent = parts.join(" \u00b7 ");
  } catch (e) {
    /* non-fatal */
  }
}

// --- bootstrap -------------------------------------------------------------

function init() {
  loadDues();
  loadCalls();
  loadConfig();
  // Light periodic refresh of the calls list.
  setInterval(loadCalls, 10000);
}

document.addEventListener("DOMContentLoaded", init);
