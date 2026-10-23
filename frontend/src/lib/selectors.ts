import type { CallRecord, ConversationTurn, FirstOutputTiming, NovaResponseTurn, ResponseWindow, ToolCall, Turn } from "./api";

export interface ChatSegment {
  index: number;
  speaker: "user" | "assistant";
  text: string;
  transcriptFinalMs: number | null;
  transcriptIntervalMs: number | null;
  ttfaMs: number | null;
  textTtftMs: number | null;
  callerReplyGapMs: number | null;
  responseWindow: ResponseWindow | null;
  firstOutputTiming: FirstOutputTiming | null;
  interrupted: boolean;
  toolCalls: ToolCall[];
}

export function toolExecutionDetails(call: ToolCall, phaseHint: "in_call" | "post_call" = "in_call") {
  const phase = call.phase === "post_call" || call.off_path === true
    ? "post-call"
    : call.phase === "in_call" || phaseHint === "in_call" ? "in-call" : "post-call";
  const verifiedAsync = call.source === "measured_execution" && call.kind === "async" &&
    (call.execution_mode === "thread_offload" || call.execution_mode === "event_loop");
  const label = verifiedAsync ? `async · ${phase}` : `${phase} · execution not recorded`;
  const explanation = verifiedAsync
    ? call.execution_mode === "thread_offload"
      ? "Execution runs asynchronously, with blocking work moved to a worker thread."
      : "Execution runs asynchronously on the event loop."
    : "This invocation has no verified execution metadata. Its phase does not establish sync or async execution.";
  return {
    label,
    verifiedAsync,
    title: explanation + (call.model_continuation === "native_async" ? " Tool completion uses the model's native async continuation." : "") +
      (call.completion_semantics === "teardown_scheduled" ? " This completion schedules call teardown; it does not confirm that the phone call has already ended." : "") +
      (call.association?.endsWith("_estimate") ? " This turn match is estimated from recorded event times, not a model-provided link between the tool and these words." : ""),
  };
}

function measured(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

// Old response records have relative offsets. Reconstruct only boundaries that
// were actually measured, without changing the stored call or borrowing audio
// timing as text timing.
export function normalizeFirstOutputTiming(source: NovaResponseTurn | ResponseWindow | FirstOutputTiming, index = 0): FirstOutputTiming | null {
  const endSource = "boundary_source" in source ? source.boundary_source : "end_source" in source ? source.end_source : null;
  const reason = "stop_reason" in source ? source.stop_reason : null;
  const boundarySource = endSource ?? (reason === "incomplete" ? "session_end" : reason === "next_response_start" || reason === "connection_restart" ? reason : "response_stop");
  if (!["response_stop", "next_response_start", "session_end", "connection_restart"].includes(boundarySource)) return null;
  const start = measured(source.start_ms);
  const duration = "duration_ms" in source ? measured(source.duration_ms) : null;
  const end = ("boundary_ms" in source ? measured(source.boundary_ms) : measured(source.end_ms)) ?? (start !== null && duration !== null ? start + duration : null);
  if (start === null || end === null || end < start) return null;
  const marker = (absolute: unknown, offset: unknown) => {
    const value = absolute === null || absolute === undefined
      ? (measured(offset) === null ? null : start + measured(offset)!)
      : measured(absolute);
    return value !== null && value >= start && value <= end ? value : null;
  };
  const firstText = marker(source.first_text_ms, source.text_ttft_ms);
  const firstAudio = marker(source.first_audio_ms, source.ttfa_ms ?? ("ttft_ms" in source ? source.ttft_ms : null));
  return {
    response_id: source.response_id,
    response_index: ("response_index" in source ? source.response_index : undefined) ?? ("index" in source ? source.index : undefined) ?? index,
    start_ms: start,
    boundary_ms: end,
    boundary_source: boundarySource as FirstOutputTiming["boundary_source"],
    first_text_ms: firstText,
    first_audio_ms: firstAudio,
    text_ttft_ms: firstText === null ? null : firstText - start,
    ttfa_ms: firstAudio === null ? null : firstAudio - start,
    basis: "application_event_receipt",
    association: "transcript_final_within_observed_boundary",
  };
}

export function normalizeResponseWindow(source: NovaResponseTurn | ResponseWindow | FirstOutputTiming, index = 0): ResponseWindow | null {
  const timing = normalizeFirstOutputTiming(source, index);
  if (!timing || timing.boundary_source !== "response_stop") return null;
  return {
    response_id: timing.response_id,
    response_index: timing.response_index,
    start_ms: timing.start_ms,
    end_ms: timing.boundary_ms,
    duration_ms: timing.boundary_ms - timing.start_ms,
    first_text_ms: timing.first_text_ms,
    first_audio_ms: timing.first_audio_ms,
    text_ttft_ms: timing.text_ttft_ms,
    ttfa_ms: timing.ttfa_ms,
    basis: "application_event_receipt",
    association: "contains_transcript_final",
  };
}

type TimedTurn = Turn & ConversationTurn;
const timingKey = (timing: FirstOutputTiming) => `${timing.response_index}:${timing.start_ms}:${timing.boundary_ms}`;
const finalTime = (turn: TimedTurn) => turn.transcript_final_ms === undefined ? measured(turn.start_ms) : measured(turn.transcript_final_ms);
const containsFinal = (timing: FirstOutputTiming, final: number) => timing.start_ms <= final && final <= timing.boundary_ms &&
  [timing.first_text_ms, timing.first_audio_ms].some((marker) => typeof marker === "number" && marker <= final);

function timedSegments(record: CallRecord, source: TimedTurn[], grouped: boolean): ChatSegment[] {
  const timings = (record.nova_response_turns ?? [])
    .map(normalizeFirstOutputTiming)
    .filter((timing): timing is FirstOutputTiming => timing !== null);
  const claimed = new Set<string>();
  const turns = source.slice().sort((a, b) => (measured(a.start_ms) ?? finalTime(a) ?? 0) - (measured(b.start_ms) ?? finalTime(b) ?? 0) || (a.index ?? 0) - (b.index ?? 0));
  return turns.map((turn, i) => {
    const speaker = turn.speaker === "assistant" ? "assistant" : "user";
    const timestamp = finalTime(turn);
    const hasWindow = Object.prototype.hasOwnProperty.call(turn, "response_window");
    const hasFirstOutput = Object.prototype.hasOwnProperty.call(turn, "first_output_timing");
    let output: FirstOutputTiming | null = null;
    let window: ResponseWindow | null = null;
    if (speaker === "assistant" && timestamp !== null) {
      const candidate = hasFirstOutput
        ? (turn.first_output_timing ? normalizeFirstOutputTiming(turn.first_output_timing) : null)
        : hasWindow
          ? (turn.response_window ? normalizeFirstOutputTiming(turn.response_window) : null)
          : timings.filter((timing) => containsFinal(timing, timestamp))
          .sort((a, b) => b.start_ms - a.start_ms || (b.response_index ?? 0) - (a.response_index ?? 0))[0] ?? null;
      // Select the newest containing response before checking ownership. An
      // already claimed response must not redirect a later final to an older one.
      if (candidate && containsFinal(candidate, timestamp) && !claimed.has(timingKey(candidate))) {
        output = candidate;
        window = hasWindow ? (turn.response_window ? normalizeResponseWindow(turn.response_window) : null) : normalizeResponseWindow(candidate);
        claimed.add(timingKey(candidate));
      }
    }
    const previous = turns[i - 1];
    const callerFinal = previous?.speaker === "user" && finalTime(previous) !== null
      ? (grouped ? measured(previous.end_ms) ?? finalTime(previous) : finalTime(previous))
      : null;
    const firstAudio = measured(output?.first_audio_ms);
    const gap = callerFinal !== null && firstAudio !== null && firstAudio >= callerFinal
      ? firstAudio - callerFinal
      : null;
    const directLegacyTiming = !hasWindow && !hasFirstOutput && !record.nova_response_turns?.length;
    return {
      index: turn.index ?? i,
      speaker,
      text: turn.text ?? "",
      transcriptFinalMs: timestamp,
      transcriptIntervalMs: timestamp === null ? null : measured(turn.transcript_interval_ms) ?? (grouped ? measured(turn.turn_elapsed_ms) ?? measured(turn.total_ms) : measured(turn.duration_ms)),
      ttfaMs: speaker === "assistant" ? (output?.ttfa_ms ?? (directLegacyTiming ? measured(turn.ttfa_ms) ?? measured(turn.ttft_ms) : null)) : null,
      textTtftMs: speaker === "assistant" ? (output?.text_ttft_ms ?? (directLegacyTiming ? measured(turn.text_ttft_ms) : null)) : null,
      callerReplyGapMs: gap,
      responseWindow: window,
      firstOutputTiming: output,
      interrupted: Boolean(turn.interrupted),
      toolCalls: Array.isArray(turn.tool_calls) ? turn.tool_calls : [],
    };
  });
}

function transcriptTurns(record: CallRecord): TimedTurn[] {
  const transcripts = Array.isArray(record.transcripts) ? record.transcripts : [];
  const finals = transcripts.filter((t) => t.is_final);
  return (finals.length ? finals : transcripts).slice().sort((a, b) => (a.t_ms ?? 0) - (b.t_ms ?? 0))
    .filter((t) => t.text).map((t, index) => ({ index, speaker: t.role, text: t.text, start_ms: t.t_ms, transcript_final_ms: t.is_final ? t.t_ms : null }));
}

export function chatSegments(record: CallRecord): ChatSegment[] {
  if (record.conversation_turns?.length) return timedSegments(record, record.conversation_turns, true);
  if (record.turns?.length) return timedSegments(record, record.turns, false);
  return timedSegments(record, transcriptTurns(record), false);
}

// Per-utterance rows are matched independently. Grouped conversation indexes
// are not utterance indexes, especially when Nova has several finals in a row.
export function utteranceSegments(record: CallRecord): ChatSegment[] {
  if (record.turns?.length) return timedSegments(record, record.turns, false);
  const transcripts = transcriptTurns(record);
  return transcripts.length ? timedSegments(record, transcripts, false) : chatSegments(record);
}

/** Collect execution evidence once, preferring the measured call-level record.
 * Duplicate copies in grouped turns and invocation events are not new jobs.
 * For records without IDs, occurrence counts retain repeated identical jobs
 * within one source while matching their copied occurrences in other sources.
 */
export function inCallTools(record: CallRecord): ToolCall[] {
  const sources: ToolCall[][] = [
    record.tool_executions ?? [],
    (record.turns ?? []).flatMap((turn) => (turn as TimedTurn).tool_calls ?? []),
    (record.conversation_turns ?? []).flatMap((turn) => turn.tool_calls ?? []),
    (record.events_log ?? []).filter((event) => event.event_type === "tool_invocation").map((event) => ({
      tool: typeof event.tool === "string" ? event.tool : null,
      tool_use_id: typeof event.tool_use_id === "string" ? event.tool_use_id : null,
      t_ms: event.t_ms, phase: "in_call", kind: "unknown", source: "invocation_only",
    })),
  ];
  const calls = new Map<string, ToolCall>();
  const aliases = new Map<string, string>();
  sources.forEach((source) => {
    const occurrences = new Map<string, number>();
    source.forEach((call) => {
      if (call.phase === "post_call" || call.off_path) return;
      const signature = JSON.stringify([call.tool ?? null, measured(call.t_ms)]);
      const ordinal = occurrences.get(signature) ?? 0;
      occurrences.set(signature, ordinal + 1);
      const alias = `${signature}:${ordinal}`;
      const aliasKey = aliases.get(alias);
      const key = call.tool_use_id ? `id:${call.tool_use_id}` : aliasKey ?? `event:${alias}`;
      let previous = calls.get(key);
      // An older measured record can omit an ID supplied by its grouped copy.
      // Promote that same occurrence to the ID without losing the measured
      // outcome or coalescing two different nonempty IDs at an equal timestamp.
      if (!previous && call.tool_use_id && aliasKey) {
        const unidentified = calls.get(aliasKey);
        if (unidentified && !unidentified.tool_use_id) {
          previous = { ...unidentified, tool_use_id: call.tool_use_id };
          calls.delete(aliasKey);
          for (const [name, value] of aliases) if (value === aliasKey) aliases.set(name, key);
        }
      }
      // Higher-priority measurement fields win over historical invocation data.
      calls.set(key, previous ? { ...call, ...previous } : { ...call });
      if (!aliases.has(alias)) aliases.set(alias, key);
    });
  });
  return [...calls.values()].sort((a, b) => (measured(a.t_ms) ?? Infinity) - (measured(b.t_ms) ?? Infinity));
}

/** Place tools beside individual utterances without borrowing grouped indexes.
 * A containing response gives the closest recorded context. Otherwise a job
 * following a caller belongs beside the next published assistant reply; a job
 * following an assistant stays beside that reply. Both are timestamp estimates.
 * A job's later completion does not change the turn where it was admitted.
 */
export function turnMetrics(record: CallRecord): { turns: ChatSegment[]; unassignedTools: ToolCall[] } {
  const turns = utteranceSegments(record).map((turn) => ({ ...turn, toolCalls: [] as ToolCall[] }));
  const unassignedTools: ToolCall[] = [];
  const callEnd = measured(record.total_e2e_ms);
  for (const call of inCallTools(record)) {
    const at = measured(call.t_ms);
    if (at === null || (callEnd !== null && at > callEnd)) {
      unassignedTools.push(call);
      continue;
    }
    const containing = turns.filter((turn) => turn.speaker === "assistant" && turn.firstOutputTiming &&
      turn.firstOutputTiming.start_ms <= at && at <= turn.firstOutputTiming.boundary_ms)
      .sort((a, b) => b.firstOutputTiming!.start_ms - a.firstOutputTiming!.start_ms ||
        (b.firstOutputTiming!.response_index ?? 0) - (a.firstOutputTiming!.response_index ?? 0));
    let target = containing[0];
    let association = "response_event_time_estimate";
    if (!target) {
      association = "transcript_event_time_estimate";
      const published = turns.filter((turn) => turn.transcriptFinalMs !== null && turn.transcriptFinalMs <= at);
      const preceding = published[published.length - 1];
      target = preceding?.speaker === "assistant" ? preceding : turns.find((turn) =>
        turn.speaker === "assistant" && turn.transcriptFinalMs !== null && turn.transcriptFinalMs >= at)!;
    }
    if (target) target.toolCalls.push({ ...call, association });
    else unassignedTools.push(call);
  }
  return { turns, unassignedTools };
}

/** Arrival-time arithmetic only: no claim about model encoding or playback. */
export function firstAudioValues(ttfaMs: number | null, textTtftMs: number | null) {
  const audio = measured(ttfaMs);
  const text = measured(textTtftMs);
  return {
    text,
    gap: text !== null && audio !== null && text <= audio ? audio - text : null,
    audio,
  };
}

/** Only a completed, matching response can supply the total-duration axis. */
export function responseTimeline(segment: Pick<ChatSegment, "ttfaMs" | "textTtftMs" | "responseWindow" | "firstOutputTiming">) {
  const values = firstAudioValues(segment.ttfaMs, segment.textTtftMs);
  const window = segment.responseWindow;
  const output = segment.firstOutputTiming;
  const sameResponse = window && output && window.start_ms === output.start_ms &&
    window.response_index === output.response_index && window.response_id === output.response_id &&
    output.boundary_source === "response_stop" && window.end_ms === output.boundary_ms;
  const duration = sameResponse ? measured(window.duration_ms) : null;
  const inBounds = duration !== null &&
    [values.text, values.audio].every((value) => value === null || value <= duration);
  return { ...values, duration: inBounds ? duration : null };
}

export function connectionRestarts(record: CallRecord): number {
  return (record.events_log ?? []).filter((e) => String(e.event_type) === "connection_restart").length;
}

export function intelligenceFieldText(record: CallRecord | null, field: string, value?: string | null): string {
  if (value?.trim()) return value;
  const status = record?.intelligence_metadata?.field_status?.[field];
  if (status === "not_discussed") return "Not discussed";
  if (status === "uncertain") return "Not enough evidence";
  if (status === "needs_review") return "Needs date confirmation";
  if (status === "missing" || status === "invalid") return field === "next_best_action" ? "Recommendation unavailable" : "Not captured";
  return field === "next_best_action" ? "No recommendation recorded" : "Not recorded";
}

export function isIntelligenceTerminal(status?: string | null): boolean {
  return status === "ready" || status === "partial" || status === "failed";
}
