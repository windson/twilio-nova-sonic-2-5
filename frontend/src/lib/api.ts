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
  // Absolute wall-clock start time (UTC ISO-8601) used to join calls
  // under their due record and render WHEN each call happened.
  created_at?: string | null;
  total_e2e_ms?: number | null;
  nova_total_ms?: number | null;
  // accurate-named transport ESTIMATE; twilio_latency_ms is the
  // deprecated alias (same value) kept until the backend drops it.
  transport_delay_estimate_ms?: number | null;
  twilio_latency_ms?: number | null;
  ttft_ms?: number | null;
  ttfa_ms?: number | null;
  text_ttft_ms?: number | null;
  turn_count?: number | null;
  nova_response_count?: number | null;
  barge_in_count?: number | null;
  disposition?: string | null;
  intelligence_status?: string | null;
  // Backend-built per-call CloudWatch dashboard deep-link (config-built,
  // scoped to this call_sid). The UI prefers this over client-side building.
  dashboard_url?: string | null;
}

export interface Turn {
  index?: number;
  speaker?: string; // 'user' | 'assistant'
  text?: string;
  start_ms?: number | null;
  duration_ms?: number | null;
  transcript_final_ms?: number | null;
  transcript_interval_ms?: number | null;
  transcript_interval_source?: string | null;
  text_ttft_ms?: number | null;
  response_window?: ResponseWindow | null;
  first_output_timing?: FirstOutputTiming | null;
  caller_reply_gap_ms?: number | null;
  caller_reply_gap_source?: string | null;
}

export interface ResponseWindow {
  response_id?: string;
  response_index?: number;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  first_text_ms?: number | null;
  first_audio_ms?: number | null;
  text_ttft_ms?: number | null;
  ttfa_ms?: number | null;
  basis?: "application_event_receipt" | string;
  association?: "contains_transcript_final" | string;
}

export interface FirstOutputTiming {
  response_id?: string;
  response_index?: number;
  start_ms: number;
  first_text_ms?: number | null;
  first_audio_ms?: number | null;
  text_ttft_ms?: number | null;
  ttfa_ms?: number | null;
  boundary_ms: number;
  boundary_source: "response_stop" | "next_response_start" | "session_end" | "connection_restart";
  basis?: string;
  association?: string;
}

export interface NovaResponseTurn {
  response_id?: string;
  index?: number;
  start_ms?: number | null;
  end_ms?: number | null;
  duration_ms?: number | null;
  first_text_ms?: number | null;
  first_audio_ms?: number | null;
  text_ttft_ms?: number | null;
  ttfa_ms?: number | null;
  // Historical alias for audio, never text TTFT.
  ttft_ms?: number | null;
  end_source?: string | null;
  stop_reason?: string | null;
}

// Invocation phase and execution mode are separate facts. Older records do
// not have execution metadata and must not be relabelled as verified async.
export interface ToolCall {
  tool?: string | null;
  kind?: "sync" | "async" | string;
  phase?: "in_call" | "post_call" | string;
  execution_mode?: "thread_offload" | "event_loop" | string;
  status?: "running" | "success" | "error" | "cancelled" | string;
  tool_use_id?: string | null;
  completed_t_ms?: number | null;
  model_continuation?: string | null;
  error_type?: string | null;
  source?: string | null;
  completion_semantics?: string | null;
  result_delivered?: boolean;
  association?: "response_event_time_estimate" | "transcript_event_time_estimate" | string;
  cancel_requested?: boolean;
  t_ms?: number | null;
  duration_ms?: number | null;
  // Present and true on async (post-call) entries: they do NOT add call
  // latency.
  off_path?: boolean;
  // on async date-resolution entries, which tier of the
  // hybrid resolver produced the answer ('deterministic' | 'code_interpreter'
  // | 'unresolved'), surfaced on the async tool chip so the resolution path is
  // visible in the turn-by-turn view.
  resolved_via?: string | null;
}

// An inline barge-in marker interleaved into the conversation timeline.
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
  transcript_final_ms?: number | null;
  transcript_interval_ms?: number | null;
  transcript_interval_source?: string | null;
  response_window?: ResponseWindow | null;
  first_output_timing?: FirstOutputTiming | null;
  caller_reply_gap_source?: string | null;
  // Accurate names (the legacy total_ms / latency_ms aliases carry the
  // same values and are kept until the backend drops them).
  turn_elapsed_ms?: number | null;
  total_ms?: number | null;
  // Observed caller-final transcript receipt to first reply audio receipt.
  // Historical aliases without source metadata can have a wrong association.
  caller_reply_gap_ms?: number | null;
  response_wait_ms?: number | null;
  latency_ms?: number | null;
  transcript_span_ms?: number | null;
  ttft_ms?: number | null;
  ttfa_ms?: number | null;
  text_ttft_ms?: number | null;
  ttfa_source?: string | null;
  ttft_source?: string | null;
  // assistant turns interrupted by a barge-in.
  interrupted?: boolean;
  barge_in_ms?: number[];
  // In-call invocations; execution metadata determines sync/async semantics.
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

// authentic Twilio Call-resource facts fetched off-path post-call.
// Advanced QoS (RTT/jitter/packet-loss/MOS) is a paid add-on and is never
// inferred; advanced_qos_available is false because this integration omits it.
export interface TwilioInsights {
  available?: boolean;
  source?: string; // 'pending' | 'twilio_call_resource' | 'unavailable'
  advanced_qos_available?: boolean;
  advanced_qos_reason?: string | null;
  fields?: Record<string, unknown>;
  error?: string;
}

export interface SonicCostBreakdown {
  direction: "input" | "output";
  modality: "text" | "audio";
  tokens: number;
  rate_per_million_usd: string;
  cost_usd: string;
  sku?: string;
  rate_code?: string;
}

export interface SonicCostEstimate {
  schema_version?: number;
  status: "estimated" | "partial" | "unavailable" | "pending";
  reason?: string | null;
  model_id?: string | null;
  model_name?: string;
  region?: string | null;
  currency?: "USD";
  total_cost_usd?: string | null;
  observed_cost_usd?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  usage_source?: string;
  usage_complete?: boolean;
  usage_event_count?: number | null;
  breakdown?: SonicCostBreakdown[];
  pricing?: {
    source?: string;
    source_url?: string;
    catalog_version?: string;
    effective_at?: string;
    published_at?: string;
    checked_at?: string;
    basis?: string;
  };
  exclusions?: string[];
}

export interface CallRecord {
  termination?: {
    outcome: string;
    reason?: string | null;
    attempt_count: number;
    telephone_completion_confirmed: boolean;
    timestamps?: Record<string, string>;
    closing_handshake_ms?: number;
  };
  call_sid?: string;
  account_id?: string;
  // Absolute wall-clock start time (UTC ISO-8601).
  created_at?: string | null;
  total_e2e_ms?: number | null;
  nova_total_ms?: number | null;
  non_nova_ms?: number | null;
  // accurate-named transport ESTIMATE; twilio_latency_ms is the
  // deprecated alias (same value).
  transport_delay_estimate_ms?: number | null;
  twilio_latency_ms?: number | null;
  ttft_ms?: number | null;
  ttfa_ms?: number | null;
  text_ttft_ms?: number | null;
  turn_count?: number | null;
  nova_response_count?: number | null;
  barge_in_count?: number | null;
  currency?: string | null;
  usage?: Record<string, unknown>;
  sonic_cost?: SonicCostEstimate | null;
  turns?: Turn[];
  conversation_turns?: ConversationTurn[];
  nova_response_turns?: NovaResponseTurn[];
  transcripts?: Transcript[];
  events_log?: EventLogEntry[];
  // authentic Twilio Call-resource facts (pending until enrichment).
  twilio_insights?: TwilioInsights | null;
  // Call-level async (post-call, off-path) tool calls.
  async_tool_calls?: ToolCall[];
  // Instrumented in-call executions, including those without a turn match.
  tool_executions?: ToolCall[];
  // inline barge-in timeline markers (ordered by t_ms).
  barge_in_events?: BargeInEvent[];
  disposition?: string | null;
  structured_data?: StructuredData | null;
  payment_date_resolution?: {
    reference_date?: string | null;
    reference_timezone?: string | null;
    phrase?: string | null;
    status?: string | null;
    requires_review?: boolean;
    reason?: string | null;
  } | null;
  next_best_action?: string | null;
  intelligence_status?: string | null;
  intelligence_metadata?: {
    model_id?: string;
    api?: string;
    status?: "ready" | "partial" | "failed";
    attempts?: number;
    issues?: string[];
    field_status?: Record<string, "extracted" | "not_discussed" | "missing" | "invalid" | "uncertain" | "recommended" | "resolved" | "needs_review">;
    duration_ms?: number;
    usage?: Record<string, unknown>;
    stop_reason?: string;
  } | null;
  collection_paused?: boolean;
  escalated?: boolean;
  ptp_sms_status?: string | null;
  ptp_sms_to?: string | null;
  triggers?: string[];
  // Backend-built per-call CloudWatch dashboard deep-link.
  dashboard_url?: string | null;
}

export interface AppConfig {
  nova_model_id?: string;
  conversation_languages?: string[];
  polyglot_voice?: string;
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

// Render an absolute wall-clock timestamp (local, human readable) for
// The calls-under-dues "WHEN" column. Returns the dash placeholder for a
// missing/invalid value.
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString();
}

// Render a coarse relative time like "2h ago" / "just now" for the
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

// Mirror the backend's named CloudWatch dashboard link as a fallback when
// dashboard_url is absent. The operator enters the separately displayed call
// SID in the dashboard's pattern variable to scope its log widgets.
const DEFAULT_STACK_NAME = "ar-collections-nova-sonic";

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
  // Signature parity with the backend helper. The dashboard takes the call
  // SID through its input field rather than the named-dashboard URL.
  void callSid;
  const region = awsRegion || "us-east-1";
  const name = `${stackNameFromLogGroup(appLogGroup)}-per-call`;
  // The documented fragment contains only the dashboard name.
  const base = `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}`;
  return `${base}#dashboards:name=${name}`;
}
