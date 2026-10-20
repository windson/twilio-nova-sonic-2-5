// Typed client + models for the FastAPI REST API. The response SHAPES here
// mirror the backend exactly (app/main.py); this is a pure frontend
// replacement and does not change any contract.

export interface DueRecord {
  account_id?: string;
  debtor_name?: string;
  amount_due?: number | null;
  currency?: string | null;
  due_date?: string | null;
  status?: string | null;
}

export interface CallSummary {
  call_sid?: string;
  account_id?: string;
  // ASK 3: absolute wall-clock start time (UTC ISO-8601) used to join calls
  // under their due record and render WHEN each call happened.
  created_at?: string | null;
  total_e2e_ms?: number | null;
  nova_total_ms?: number | null;
  // ASK 6: accurate-named transport ESTIMATE; twilio_latency_ms is the
  // deprecated alias (same value) kept until the backend drops it.
  transport_delay_estimate_ms?: number | null;
  twilio_latency_ms?: number | null;
  ttft_ms?: number | null;
  turn_count?: number | null;
  nova_response_count?: number | null;
  barge_in_count?: number | null;
  disposition?: string | null;
  intelligence_status?: string | null;
  // ASK 4: backend-built per-call CloudWatch dashboard deep-link (config-built,
  // scoped to this call_sid). The UI prefers this over client-side building.
  dashboard_url?: string | null;
}

export interface Turn {
  index?: number;
  speaker?: string; // 'user' | 'assistant'
  text?: string;
  start_ms?: number | null;
  duration_ms?: number | null;
}

// ASK 5: a tool invocation attached to a conversation turn (sync, in-call) or
// to the call record (async, post-call, off-path).
export interface ToolCall {
  tool?: string | null;
  kind?: "sync" | "async" | string;
  t_ms?: number | null;
  duration_ms?: number | null;
  // Present and true on async (post-call) entries: they do NOT add call
  // latency.
  off_path?: boolean;
  // FEAT-003/FEAT-005: on async date-resolution entries, which tier of the
  // hybrid resolver produced the answer ('deterministic' | 'code_interpreter'
  // | 'unresolved'), surfaced on the async tool chip so the resolution path is
  // visible in the turn-by-turn view.
  resolved_via?: string | null;
}

// ASK 7: an inline barge-in marker interleaved into the conversation timeline.
export interface BargeInEvent {
  type?: string; // 'barge_in'
  t_ms?: number | null;
}

export interface ConversationTurn {
  index?: number;
  speaker?: string;
  start_ms?: number | null;
  end_ms?: number | null;
  text?: string;
  // ASK 1 accurate names (the legacy total_ms / latency_ms aliases carry the
  // same values and are kept until the backend drops them).
  turn_elapsed_ms?: number | null;
  total_ms?: number | null;
  // FEAT-004: caller->reply gap (time from the caller finishing speaking to
  // the agent beginning its reply), present only when a genuine caller turn
  // precedes this assistant turn. response_wait_ms / latency_ms are kept as
  // backward-compatible aliases carrying the same value.
  caller_reply_gap_ms?: number | null;
  response_wait_ms?: number | null;
  latency_ms?: number | null;
  transcript_span_ms?: number | null;
  ttft_ms?: number | null;
  ttft_source?: string | null;
  // ASK 7: assistant turns interrupted by a barge-in.
  interrupted?: boolean;
  barge_in_ms?: number[];
  // ASK 5: sync in-call tool invocations attached to this turn.
  tool_calls?: ToolCall[];
}

export interface Transcript {
  role?: string;
  text?: string;
  is_final?: boolean;
  t_ms?: number | null;
}

export interface EventLogEntry {
  event_type?: string;
  t_ms?: number | null;
  [key: string]: unknown;
}

export interface StructuredData {
  payment_specifics?: {
    promised_amount?: number | null;
    agreed_date?: string | null;
    preferred_payment_method?: string | null;
  };
  contextual_data?: {
    reason_for_delinquency?: string | null;
    customer_sentiment?: string | null;
  };
  [key: string]: unknown;
}

// ASK 2/6: authentic Twilio Call-resource facts fetched off-path post-call.
// Advanced QoS (RTT/jitter/packet-loss/MOS) is a paid add-on and is never
// fabricated; advanced_qos_available is false on the trial account.
export interface TwilioInsights {
  available?: boolean;
  source?: string; // 'pending' | 'twilio_call_resource' | 'unavailable'
  advanced_qos_available?: boolean;
  advanced_qos_reason?: string | null;
  fields?: Record<string, unknown>;
  error?: string;
}

export interface CallRecord {
  call_sid?: string;
  account_id?: string;
  // ASK 3: absolute wall-clock start time (UTC ISO-8601).
  created_at?: string | null;
  total_e2e_ms?: number | null;
  nova_total_ms?: number | null;
  non_nova_ms?: number | null;
  // ASK 6: accurate-named transport ESTIMATE; twilio_latency_ms is the
  // deprecated alias (same value).
  transport_delay_estimate_ms?: number | null;
  twilio_latency_ms?: number | null;
  ttft_ms?: number | null;
  turn_count?: number | null;
  nova_response_count?: number | null;
  barge_in_count?: number | null;
  currency?: string | null;
  usage?: Record<string, unknown>;
  turns?: Turn[];
  conversation_turns?: ConversationTurn[];
  transcripts?: Transcript[];
  events_log?: EventLogEntry[];
  // ASK 2/6: authentic Twilio Call-resource facts (pending until enrichment).
  twilio_insights?: TwilioInsights | null;
  // ASK 5: call-level async (post-call, off-path) tool calls.
  async_tool_calls?: ToolCall[];
  // ASK 7: inline barge-in timeline markers (ordered by t_ms).
  barge_in_events?: BargeInEvent[];
  disposition?: string | null;
  structured_data?: StructuredData | null;
  next_best_action?: string | null;
  intelligence_status?: string | null;
  collection_paused?: boolean;
  escalated?: boolean;
  ptp_sms_status?: string | null;
  ptp_sms_to?: string | null;
  triggers?: string[];
  // ASK 4: backend-built per-call CloudWatch dashboard deep-link.
  dashboard_url?: string | null;
}

export interface AppConfig {
  nova_model_id?: string;
  aws_region?: string;
  app_log_group?: string;
  extraction_model_id?: string;
  code_interpreter?: string;
  code_interpreter_id?: string;
  twilio_trial_mode?: boolean;
  twilio_configured?: boolean;
  public_base_url_set?: boolean;
  [key: string]: unknown;
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const detail = b.detail as unknown;
    if (detail && typeof detail === "object") {
      const d = detail as Record<string, unknown>;
      if (typeof d.message === "string") return d.message;
      return JSON.stringify(detail);
    }
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* ignore */
    }
    throw new Error(errorMessage(body, res.statusText));
  }
  return (await res.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      /* ignore */
    }
    throw new Error(errorMessage(data, res.statusText));
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Formatting + domain helpers ported 1:1 from the vanilla web/app.js so the
// new UI behaves identically.
// ---------------------------------------------------------------------------

export const DASH = "\u2013";

export function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  const n = Number(value);
  if (Number.isNaN(n)) return DASH;
  if (n >= 1000) return (n / 1000).toFixed(2) + " s";
  return Math.round(n) + " ms";
}

export function fmtMoney(
  amount: number | null | undefined,
  currency?: string | null
): string {
  if (amount === null || amount === undefined) return DASH;
  const cur = currency || "USD";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: cur,
    }).format(Number(amount));
  } catch {
    return `${Number(amount).toFixed(2)} ${cur}`;
  }
}

export function fmtText(value: unknown): string {
  if (value === null || value === undefined || value === "") return DASH;
  return String(value);
}

// ASK 3: render an absolute wall-clock timestamp (local, human readable) for
// the calls-under-dues "WHEN" column. Returns the dash placeholder for a
// missing/invalid value.
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString();
}

// ASK 3: render a coarse relative time like "2h ago" / "just now" for the
// calls-under-dues "WHEN" column (shown alongside the absolute timestamp).
export function fmtRelativeTime(
  iso: string | null | undefined,
  now: number = Date.now()
): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diffMs = now - t;
  const future = diffMs < 0;
  const abs = Math.abs(diffMs);
  const sec = Math.round(abs / 1000);
  const min = Math.round(sec / 60);
  const hr = Math.round(min / 60);
  const day = Math.round(hr / 24);
  let text: string;
  if (sec < 45) text = "just now";
  else if (min < 60) text = `${min}m`;
  else if (hr < 24) text = `${hr}h`;
  else text = `${day}d`;
  if (text === "just now") return text;
  return future ? `in ${text}` : `${text} ago`;
}

// Map a snake_case disposition tag to a human-readable label (ported from
// web/app.js DISPOSITION_LABELS).
const DISPOSITION_LABELS: Record<string, string> = {
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

export function dispositionLabel(disposition?: string | null): string {
  if (!disposition) return "";
  if (DISPOSITION_LABELS[disposition]) return DISPOSITION_LABELS[disposition];
  return String(disposition)
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

// ASK 4 / D3: build the deep-link that OPENS the CONSOLIDATED per-call
// CloudWatch DASHBOARD (<stack>-per-call, provisioned in infra/template.yaml)
// with its multi-widget view scoped to a single call_sid: per-turn timeline,
// TTFT + turns, token usage, barge-ins, tool calls and Twilio/transport. The
// dashboard's log widgets filter on a pattern dashboard variable whose token
// (CALL_SID_TOKEN) this link overrides with the call_sid, so every widget loads
// already filtered to the call. The backend returns this same URL on each call
// record (dashboard_url); this client-side builder mirrors that construction
// exactly and is used as a fallback when the backend field is absent.
//
// Encoding mirrors app/dashboard.py: each literal is percent-encoded then "%"
// is rewritten to the console's "*" escape; "?"/"=" in the fragment use the
// console's "$3F"/"$3D" escapes; the params value is the console's RISON-like
// "~(...)" token tree carrying the dashboard-variable override.
const CALL_SID_TOKEN = "CALL_SID_TOKEN";
const DEFAULT_STACK_NAME = "ar-collections-nova-sonic";

function consoleToken(value: string): string {
  let out = "";
  for (const ch of value) {
    if (/[A-Za-z0-9\-_.]/.test(ch)) {
      out += ch;
    } else {
      for (const byte of new TextEncoder().encode(ch)) {
        out += "*" + byte.toString(16).toUpperCase().padStart(2, "0");
      }
    }
  }
  return out;
}

// The per-call Logs Insights query the dashboard widgets are built from; kept
// mirrored with app/dashboard.py so the two stay in lock-step.
export function callDashboardQuery(callSid: string, appLogGroup: string): string {
  return (
    `SOURCE '${appLogGroup}'` +
    ` | fields t_ms, event_type, ttft_ms, turn_latency_ms, duration_ms,` +
    ` tool, role, input_tokens, output_tokens, total_tokens, message` +
    ` | filter call_sid = '${callSid}'` +
    ` | sort t_ms asc` +
    ` | limit 1000`
  );
}

// Derive the stack name from the app log group "/ecs/<stack>/app" (mirrors
// app/dashboard.stack_name_from_log_group) so the dashboard name resolves
// without an extra config field.
function stackNameFromLogGroup(appLogGroup?: string | null): string {
  if (!appLogGroup) return DEFAULT_STACK_NAME;
  const parts = appLogGroup.replace(/^\/+|\/+$/g, "").split("/");
  if (parts.length === 3 && parts[0] === "ecs" && parts[2] === "app" && parts[1]) {
    return parts[1];
  }
  return DEFAULT_STACK_NAME;
}

export function callDashboardUrl(
  callSid: string,
  awsRegion?: string | null,
  appLogGroup?: string | null
): string {
  const region = awsRegion || "us-east-1";
  const name = `${stackNameFromLogGroup(appLogGroup)}-per-call`;
  // Dashboard-variable override token tree the console reads from the URL:
  //   ~(~(name~'CALL_SID_TOKEN~value~'<call_sid>))
  const variables =
    `~(~(name~'${consoleToken(CALL_SID_TOKEN)}` +
    `~value~'${consoleToken(callSid)}))`;
  const base = `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}`;
  return `${base}#dashboards:name=${consoleToken(name)}$3Fparams$3D${variables}`;
}
