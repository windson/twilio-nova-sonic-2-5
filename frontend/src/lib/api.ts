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
  total_e2e_ms?: number | null;
  nova_total_ms?: number | null;
  twilio_latency_ms?: number | null;
  ttft_ms?: number | null;
  turn_count?: number | null;
  nova_response_count?: number | null;
  barge_in_count?: number | null;
  disposition?: string | null;
  intelligence_status?: string | null;
}

export interface Turn {
  index?: number;
  speaker?: string; // 'user' | 'assistant'
  text?: string;
  start_ms?: number | null;
  duration_ms?: number | null;
}

export interface ConversationTurn {
  index?: number;
  speaker?: string;
  start_ms?: number | null;
  end_ms?: number | null;
  text?: string;
  total_ms?: number | null;
  ttft_ms?: number | null;
  ttft_source?: string | null;
  latency_ms?: number | null;
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

export interface CallRecord {
  call_sid?: string;
  account_id?: string;
  total_e2e_ms?: number | null;
  nova_total_ms?: number | null;
  non_nova_ms?: number | null;
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
  disposition?: string | null;
  structured_data?: StructuredData | null;
  next_best_action?: string | null;
  intelligence_status?: string | null;
  collection_paused?: boolean;
  escalated?: boolean;
  ptp_sms_status?: string | null;
  ptp_sms_to?: string | null;
  triggers?: string[];
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

// Build the CloudWatch Logs console deep link scoped to a single call. This is
// a verbatim port of web/app.js cloudwatchLogsUrl: the log-group path is
// double-URL-encoded (each "/" -> "%2F" -> console "$252F"), and "?"/"="
// inside the fragment are written as the console's "$3F"/"$3D" escapes; the
// call_sid is passed as a PLAIN filterPattern term (not quoted).
export function cloudwatchLogsUrl(
  callSid: string,
  awsRegion?: string | null,
  appLogGroup?: string | null
): string {
  const region = awsRegion || "us-east-1";
  const logGroup = appLogGroup || "/ecs/ar-collections-nova-sonic/app";
  const encGroup = encodeURIComponent(logGroup).replace(/%/g, "$25");
  const filter = encodeURIComponent(callSid).replace(/%/g, "$25");
  const base = `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}`;
  return (
    `${base}#logsV2:log-groups/log-group/${encGroup}/log-events` +
    `$3FfilterPattern$3D${filter}`
  );
}
