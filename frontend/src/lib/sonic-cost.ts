import type { CallRecord } from "./api";

const DASH = "–";

/** Tiny token charges stay visible; missing values are never coerced to zero. */
export function sonicUsd(value: string | null | undefined): string {
  if (typeof value !== "string" || !/^\d+(?:\.\d+)?$/.test(value)) return DASH;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return DASH;
  if (amount > 0 && amount < 0.000000001) return "<$0.000000001";
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 9,
  }).format(amount);
}

export function sonicTokens(value: number | null | undefined): string {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value.toLocaleString("en-US") : DASH;
}

const reasons: Record<string, string> = {
  region_not_priced: "No verified price is available for this call's region.",
  model_not_priced: "This estimate covers Nova 2.5 Sonic only.",
  usage_not_recorded: "Token usage was not recorded for this call.",
  usage_not_accumulated: "The saved usage does not cover the whole call.",
  modality_counts_missing: "Separate audio and text token counts are missing.",
  modality_counts_missing_or_invalid: "Separate audio and text token counts are missing or invalid.",
  invalid_usage_totals: "The saved token totals are incomplete or invalid.",
  usage_totals_mismatch: "The saved token totals do not agree, so a cost cannot be calculated.",
  modality_totals_mismatch: "Audio and text counts do not match the saved totals.",
  unpriced_token_category: "Some recorded tokens do not have a verified price.",
};

export function sonicCostView(record: CallRecord) {
  const cost = record.sonic_cost;
  if (record.termination?.telephone_completion_confirmed === false) {
    return { state: "unavailable", label: "Cost withheld", amount: DASH,
      explanation: "Telephone disconnection has not been confirmed.", showUsage: false };
  }
  const telephoneEnded = record.termination?.telephone_completion_confirmed === true ||
    (record.twilio_insights?.source === "twilio_call_resource" &&
      ["completed", "canceled", "failed", "busy", "no-answer"].includes(
        String(record.twilio_insights.fields?.status ?? "")));
  // Older completed calls can have provider usage without a duration. Real
  // telephone completion still permits their independently backfilled cost.
  if (record.total_e2e_ms == null && !telephoneEnded) {
    return { state: "waiting", label: "After disconnection", amount: DASH,
      explanation: "Calculated after telephone disconnection is confirmed.", showUsage: false };
  }
  if (!cost || cost.status === "pending") {
    return { state: "waiting", label: "Not calculated yet", amount: DASH,
      explanation: "No cost estimate is saved for this call yet.", showUsage: false };
  }
  if (cost.status === "estimated" && cost.usage_complete === true && sonicUsd(cost.total_cost_usd) !== DASH) {
    return { state: "estimated", label: "List-price estimate", amount: sonicUsd(cost.total_cost_usd),
      explanation: "", showUsage: true };
  }
  if (cost.status === "partial" && sonicUsd(cost.observed_cost_usd) !== DASH) {
    return { state: "partial", label: "Observed usage only", amount: sonicUsd(cost.observed_cost_usd),
      explanation: "Some usage is missing. This is a subtotal, not the full call cost.", showUsage: true };
  }
  return { state: "unavailable", label: "Cost unavailable", amount: DASH,
    explanation: reasons[cost.reason ?? ""] ?? "The available usage cannot be priced reliably.", showUsage: true };
}
