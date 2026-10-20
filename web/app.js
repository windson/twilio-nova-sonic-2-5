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
  // Populated from GET /api/config so the per-call CloudWatch link uses the
  // region + log group the running service actually reports.
  awsRegion: "us-east-1",
  appLogGroup: "/ecs/ar-collections-nova-sonic/app",
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

// Build a CloudWatch Logs console "Log events" deep link scoped to a single
// call (observations #3/#4).
//
// POC approach A (no new IAM, built entirely client-side): we link to the log
// group's "Log events" view with a filterPattern on the call_sid. The
// CloudWatch console encodes this view in the URL fragment with a custom
// scheme: the log-group path is double-URL-encoded (each "/" -> "%2F" -> the
// console's "$252F"), and "?"/"=" inside the fragment are written as the
// console escapes "$3F"/"$3D". The resulting shape is:
//
//   https://<region>.console.aws.amazon.com/cloudwatch/home?region=<region>
//     #logsV2:log-groups/log-group/<enc log group>/log-events$3FfilterPattern$3D<term>
//
// where <enc log group> is encodeURIComponent(logGroup) with each "%" further
// encoded to "$25".
//
// filterPattern value: the CloudWatch Logs *log-events* filter treats a bare
// alphanumeric token as a TERM match (it matches any event containing that
// token). A Twilio call_sid ("CA" + 32 hex chars) is a single such token, so
// we pass it PLAIN rather than wrapping it in quotes. A quoted "CAxxxx" is a
// phrase filter and, for a token with no word-breaking characters, can fail to
// match; the plain term is the robust choice here.
function cloudwatchLogsUrl(callSid) {
  const region = state.awsRegion || "us-east-1";
  const logGroup = state.appLogGroup || "/ecs/ar-collections-nova-sonic/app";
  // Double-encode the log group: URL-encode, then turn "%" into the console's
  // "$25" fragment escape (so "/" -> "%2F" -> "$252F").
  const encGroup = encodeURIComponent(logGroup).replace(/%/g, "$25");
  // Plain term (no surrounding quotes) so the log-events filter does a term
  // match on the SID. Still encode in case of unexpected characters.
  const filter = encodeURIComponent(callSid).replace(/%/g, "$25");
  const base = `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}`;
  return (
    `${base}#logsV2:log-groups/log-group/${encGroup}/log-events` +
    `$3FfilterPattern$3D${filter}`
  );
}

// Map a snake_case disposition tag (the backend's CALL_DISPOSITIONS taxonomy)
// to a human-readable label for the UI. A couple of tags get an explicit label
// (e.g. the PTP abbreviation); everything else is Title Cased generically so
// new tags added to the backend still render sensibly.
const DISPOSITION_LABELS = {
  promise_to_pay: "Promise to Pay (PTP)",
  refusal_to_pay: "Refusal to Pay",
  dispute_raised: "Dispute Raised",
  inbound_callback_requested: "Inbound Callback Requested",
  wrong_number: "Wrong Number",
  no_answer: "No Answer",
  left_voicemail: "Left Voicemail",
  already_paid: "Already Paid",
  undetermined: "Undetermined",
};

function dispositionLabel(disposition) {
  if (!disposition) return "";
  if (DISPOSITION_LABELS[disposition]) return DISPOSITION_LABELS[disposition];
  return String(disposition)
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
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
      body.innerHTML = '<tr><td colspan="6" class="empty">No calls yet.</td></tr>';
      return;
    }
    body.innerHTML = "";
    for (const call of calls) {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      if (call.call_sid === state.selectedCallId) tr.classList.add("selected");
      // Compact disposition badge in the list so outcomes are visible at a
      // glance. Falls back to an em dash when extraction hasn't produced one.
      const dispCell = call.disposition
        ? `<span class="disp-tag disp-${call.disposition}">${dispositionLabel(call.disposition)}</span>`
        : '<span class="muted">\u2013</span>';
      tr.innerHTML = `
        <td class="mono">${call.call_sid || ""}</td>
        <td>${call.account_id || ""}</td>
        <td>${dispCell}</td>
        <td>${call.turn_count != null ? call.turn_count : "\u2013"}</td>
        <td>${fmtMs(call.total_e2e_ms)}</td>
        <td></td>`;
      tr.addEventListener("click", () => selectCall(call.call_sid, false));
      // Per-call CloudWatch Logs Insights deep link (observation #3). Clicking
      // it must not also select the row, so stop propagation.
      if (call.call_sid) {
        const link = document.createElement("a");
        link.href = cloudwatchLogsUrl(call.call_sid);
        link.target = "_blank";
        link.rel = "noopener";
        link.className = "cw-link";
        link.textContent = "Logs \u2197";
        link.title = "Open CloudWatch Logs filtered to this call";
        link.addEventListener("click", (ev) => ev.stopPropagation());
        tr.lastElementChild.appendChild(link);
      }
      body.appendChild(tr);
    }
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="empty">Failed to load calls: ${e.message}</td></tr>`;
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

  renderIntelligence(record);
  renderChat(record);
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

// Build an ordered chat model from the full call record. Prefer the
// conversation_turns list produced by the backend (FEAT-002); fall back to
// deriving an ordered chat from the raw transcripts for older records that
// predate that field.
function chatSegments(record) {
  const turns = record.conversation_turns;
  if (Array.isArray(turns) && turns.length) {
    return turns
      .slice()
      // Order by start_ms, then by the backend-assigned index as a stable
      // secondary key so turns lacking timestamps keep spoken order.
      .sort(
        (a, b) =>
          (a.start_ms || 0) - (b.start_ms || 0) ||
          (a.index || 0) - (b.index || 0)
      )
      .map((t) => ({
        speaker: t.speaker === "assistant" ? "assistant" : "user",
        text: t.text || "",
        totalMs: t.total_ms,
        ttftMs: t.ttft_ms,
        ttftSource: t.ttft_source,
        latencyMs: t.latency_ms,
      }));
  }
  // Fallback: order finals (preferred) by t_ms; if no finals exist for a role,
  // keep whatever transcripts we have.
  const transcripts = Array.isArray(record.transcripts) ? record.transcripts : [];
  const finals = transcripts.filter((t) => t.is_final);
  const source = finals.length ? finals : transcripts;
  return source
    .slice()
    .sort((a, b) => (a.t_ms || 0) - (b.t_ms || 0))
    .map((t) => ({
      speaker: t.role === "assistant" ? "assistant" : "user",
      text: t.text || "",
      totalMs: null,
      ttftMs: null,
      ttftSource: null,
      latencyMs: null,
    }))
    .filter((s) => s.text);
}

// Render the two inline intelligence blocks (Call Disposition; Structured
// Conversation Data & Next-Best-Action) from the live call record. Both start
// blank and fill in DURING or AFTER the call via the existing 3s poll: the
// backend extracts them off the real-time path (FEAT-002), so renderDetail
// simply reflects whatever the record carries on each tick.
//
// Fields read from the record (null/absent -> blank placeholder):
//   disposition            snake_case string | null
//   intelligence_status    'pending' | 'ready' | 'failed'
//   structured_data.payment_specifics {promised_amount, agreed_date,
//                          preferred_payment_method}
//   structured_data.contextual_data {reason_for_delinquency,
//                          customer_sentiment}
//   next_best_action       string | null
//   ptp_sms_status         'sent' | 'blocked_trial' | 'failed' | null
//   collection_paused      bool
//   escalated              bool
//   triggers               [string, ...]
const DASH = "\u2013";

function fmtText(value) {
  if (value === null || value === undefined || value === "") return DASH;
  return String(value);
}

function renderIntelligence(record) {
  // --- Block 1: Call Disposition tag + downstream-trigger status ----------
  const tag = $("disp-tag");
  const disposition = record.disposition;
  if (disposition) {
    tag.textContent = dispositionLabel(disposition);
    tag.className = `disp-tag disp-${disposition}`;
  } else {
    tag.textContent = "not yet identified";
    tag.className = "disp-tag disp-none";
  }

  // Downstream triggers line: SMS status + paused/escalated flags. Only shows
  // concrete signals; otherwise an em dash so the block reads as "uncalled".
  const triggerParts = [];
  if (record.ptp_sms_status) {
    const smsLabel = {
      sent: "payment-link SMS sent",
      blocked_trial: "payment-link SMS blocked (trial account)",
      failed: "payment-link SMS failed",
    }[record.ptp_sms_status] || `payment-link SMS ${record.ptp_sms_status}`;
    triggerParts.push(smsLabel);
  }
  if (record.collection_paused) triggerParts.push("collection paused");
  if (record.escalated) triggerParts.push("escalated to specialist");
  $("disp-triggers").textContent =
    "Downstream triggers: " + (triggerParts.length ? triggerParts.join(" \u00b7 ") : DASH);

  // --- Block 2: Structured data + Next-Best-Action ------------------------
  const sd = record.structured_data || {};
  const pay = sd.payment_specifics || {};
  const ctx = sd.contextual_data || {};

  $("sd-promised-amount").textContent =
    pay.promised_amount != null ? fmtMoney(pay.promised_amount, record.currency) : DASH;
  $("sd-agreed-date").textContent = fmtText(pay.agreed_date);
  $("sd-payment-method").textContent = fmtText(pay.preferred_payment_method);
  $("sd-reason").textContent = fmtText(ctx.reason_for_delinquency);
  $("sd-sentiment").textContent = fmtText(ctx.customer_sentiment);
  $("sd-nba").textContent = fmtText(record.next_best_action);

  // Extraction-status hint on the structured block: 'pending' -> extracting,
  // 'failed' -> muted note, 'ready'/absent -> no hint.
  const statusEl = $("intel-status");
  const status = record.intelligence_status;
  if (status === "pending") {
    statusEl.hidden = false;
    statusEl.textContent = "extracting\u2026";
    statusEl.className = "intel-status intel-status-pending";
  } else if (status === "failed") {
    statusEl.hidden = false;
    statusEl.textContent = "extraction unavailable";
    statusEl.className = "intel-status intel-status-failed";
  } else {
    statusEl.hidden = true;
    statusEl.textContent = "";
    statusEl.className = "intel-status";
  }
}

function renderChat(record) {
  const body = $("chat-body");
  const segments = chatSegments(record);
  if (!segments.length) {
    body.innerHTML = '<p class="empty">No conversation recorded.</p>';
    return;
  }
  body.innerHTML = "";
  for (const seg of segments) {
    const row = document.createElement("div");
    row.className = `chat-row chat-${seg.speaker}`;

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";

    const who = document.createElement("div");
    who.className = "chat-who";
    who.textContent = seg.speaker === "assistant" ? "Nova Sonic" : "Caller";

    const text = document.createElement("div");
    text.className = "chat-text";
    text.textContent = seg.text;

    const meta = document.createElement("div");
    meta.className = "chat-meta";
    const parts = [];
    if (seg.totalMs != null) parts.push(`turn ${fmtMs(seg.totalMs)}`);
    if (seg.speaker === "assistant") {
      // Latency is derivable per Sonic turn (caller-stop -> first reply), so
      // we show it whenever available. TTFT comes from the Nova response
      // window; when Nova reuses one response_id for the whole call, later
      // turns have no per-utterance audio marker, so label TTFT honestly as
      // unavailable instead of silently omitting it.
      if (seg.latencyMs != null) parts.push(`latency ${fmtMs(seg.latencyMs)}`);
      if (seg.ttftMs != null) {
        const label =
          seg.ttftSource === "response_window" ? "TTFT (response)" : "TTFT";
        parts.push(`${label} ${fmtMs(seg.ttftMs)}`);
      } else if (seg.ttftSource === "unavailable") {
        parts.push("TTFT n/a (shared response)");
      }
    }
    meta.textContent = parts.join(" \u00b7 ");

    bubble.appendChild(who);
    bubble.appendChild(text);
    if (parts.length) bubble.appendChild(meta);
    row.appendChild(bubble);
    body.appendChild(row);
  }
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
    if (cfg.aws_region) state.awsRegion = cfg.aws_region;
    if (cfg.app_log_group) state.appLogGroup = cfg.app_log_group;
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
