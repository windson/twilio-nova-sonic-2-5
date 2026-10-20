// Pure data selectors ported from web/app.js so conversation ordering and the
// latency-chart derivation behave identically.

import type { CallRecord, ConversationTurn, ToolCall } from "./api";

// ASK 1 / FEAT-004: field names use the accurate official terminology --
// turnElapsedMs = how long the turn occupied the conversation (NOT latency),
// callerReplyGapMs = time from the caller finishing speaking to the agent
// beginning its reply (present only when a genuine caller turn precedes an
// assistant turn; NOT the same thing as TTFT), ttftMs = time to first audio.
// We deliberately do NOT call any of these "latency".
export interface ChatSegment {
  speaker: "user" | "assistant";
  text: string;
  turnElapsedMs: number | null;
  ttftMs: number | null;
  ttftSource: string | null;
  callerReplyGapMs: number | null;
  // ASK 7: assistant turns interrupted by a barge-in.
  interrupted: boolean;
  // ASK 5: sync in-call tool invocations attached to this turn.
  toolCalls: ToolCall[];
}

// Prefer conversation_turns; fall back to deriving from transcripts for older
// records (ported from web/app.js chatSegments, updated for FEAT-003 field
// names with legacy aliases as fallbacks).
export function chatSegments(record: CallRecord): ChatSegment[] {
  const turns = record.conversation_turns;
  if (Array.isArray(turns) && turns.length) {
    return (turns as ConversationTurn[])
      .slice()
      .sort(
        (a, b) =>
          (a.start_ms || 0) - (b.start_ms || 0) ||
          (a.index || 0) - (b.index || 0)
      )
      .map((t) => ({
        speaker: t.speaker === "assistant" ? "assistant" : "user",
        text: t.text || "",
        // Prefer the accurate name, fall back to the deprecated alias.
        turnElapsedMs: t.turn_elapsed_ms ?? t.total_ms ?? null,
        ttftMs: t.ttft_ms ?? null,
        ttftSource: t.ttft_source ?? null,
        // Prefer the new accurate field, fall back to the deprecated aliases.
        callerReplyGapMs:
          t.caller_reply_gap_ms ?? t.response_wait_ms ?? t.latency_ms ?? null,
        interrupted: Boolean(t.interrupted),
        toolCalls: Array.isArray(t.tool_calls) ? t.tool_calls : [],
      }));
  }
  const transcripts = Array.isArray(record.transcripts) ? record.transcripts : [];
  const finals = transcripts.filter((t) => t.is_final);
  const source = finals.length ? finals : transcripts;
  return source
    .slice()
    .sort((a, b) => (a.t_ms || 0) - (b.t_ms || 0))
    .map((t) => ({
      speaker: (t.role === "assistant" ? "assistant" : "user") as "user" | "assistant",
      text: t.text || "",
      turnElapsedMs: null,
      ttftMs: null,
      ttftSource: null,
      callerReplyGapMs: null,
      interrupted: false,
      toolCalls: [] as ToolCall[],
    }))
    .filter((s) => s.text);
}

// A single point on the latency scatter/timeline: x = t_ms from call start,
// y = lane (0 = user/caller, 1 = Sonic/agent), with a label + the originating
// event type for the tooltip.
export interface LatencyPoint {
  tMs: number;
  lane: number;
  laneLabel: "Sonic" | "Caller";
  kind: string;
  label: string;
}

// Derive the scatter/timeline series from events_log[] t_ms (preferred) or
// fall back to transcripts. Sonic (assistant) events sit on lane 1, caller
// (user) events on lane 0. Only events that carry a numeric t_ms are plotted.
export function latencyPoints(record: CallRecord): LatencyPoint[] {
  const points: LatencyPoint[] = [];
  const events = Array.isArray(record.events_log) ? record.events_log : [];
  for (const ev of events) {
    const t = ev.t_ms;
    if (t === null || t === undefined || Number.isNaN(Number(t))) continue;
    const type = String(ev.event_type || "event");
    // Heuristic lane assignment from the event type / role field.
    const role = String((ev as Record<string, unknown>).role ?? "");
    const isSonic =
      role === "assistant" ||
      /sonic|assistant|nova|outbound|response|agent/i.test(type);
    points.push({
      tMs: Number(t),
      lane: isSonic ? 1 : 0,
      laneLabel: isSonic ? "Sonic" : "Caller",
      kind: type,
      label: type.replace(/_/g, " "),
    });
  }
  if (points.length === 0) {
    // Fallback: plot transcript finals on their role lane.
    const transcripts = Array.isArray(record.transcripts) ? record.transcripts : [];
    for (const tr of transcripts) {
      const t = tr.t_ms;
      if (t === null || t === undefined) continue;
      const isSonic = tr.role === "assistant";
      points.push({
        tMs: Number(t),
        lane: isSonic ? 1 : 0,
        laneLabel: isSonic ? "Sonic" : "Caller",
        kind: isSonic ? "assistant_transcript" : "user_transcript",
        label: isSonic ? "Sonic utterance" : "Caller utterance",
      });
    }
  }
  return points.sort((a, b) => a.tMs - b.tMs);
}

// Count connection_restart events for the telemetry "Connection restarts" row.
export function connectionRestarts(record: CallRecord): number {
  const events = Array.isArray(record.events_log) ? record.events_log : [];
  return events.filter((e) => String(e.event_type) === "connection_restart").length;
}
