import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// Exercise the production selectors with Node's test runner, without a browser
// shim or another test dependency. This module has only erased type imports.
const source = readFileSync(new URL("../src/lib/selectors.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { chatSegments, utteranceSegments, turnMetrics, inCallTools, normalizeFirstOutputTiming, normalizeResponseWindow, firstAudioValues, responseTimeline, intelligenceFieldText, isIntelligenceTerminal } =
  await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 0.001, `${actual} != ${expected}`);

const historicalCall = {
  conversation_turns: [
    { index: 0, speaker: "user", start_ms: 39151.022, end_ms: 39151.022, text: "Earlier question" },
    { index: 1, speaker: "assistant", start_ms: 42000, text: "Earlier reply" },
    { index: 2, speaker: "user", start_ms: 51390.981, end_ms: 51390.981, text: "Current question" },
    { index: 3, speaker: "assistant", start_ms: 53380.702, text: "Current reply", turn_elapsed_ms: 16379.488, caller_reply_gap_ms: 11926.273, ttft_ms: 440.638, text_ttft_ms: 314.428 },
  ],
  nova_response_turns: [{ index: 0, response_id: "reused", start_ms: 51077.295, duration_ms: 2303.685, ttfa_ms: 440.638, text_ttft_ms: 314.428, turn_latency_ms: 11926.273 }],
};

test("historical timing matches the current caller and separates transcript interval from response window", () => {
  const segment = chatSegments(historicalCall)[3];
  assert.equal(segment.text, "Current reply");
  near(segment.callerReplyGapMs, 126.952);
  near(segment.textTtftMs, 314.428);
  near(segment.ttfaMs, 440.638);
  near(segment.transcriptIntervalMs, 16379.488);
  near(segment.responseWindow.duration_ms, 2303.685);
  const values = firstAudioValues(segment.ttfaMs, segment.textTtftMs);
  near(values.text, 314.428);
  near(values.gap, 126.210);
  near(values.audio, values.text + values.gap);
  near(responseTimeline(segment).duration, 2303.685);
});

test("timeline total requires the matching completed response, while measurements survive missing ends", () => {
  const segment = chatSegments(historicalCall)[3];
  const timeline = responseTimeline(segment);
  near(timeline.text, 314.428);
  near(timeline.gap, 126.210);
  near(timeline.audio, 440.638);
  for (const responseWindow of [null, { ...segment.responseWindow, start_ms: 0 }, { ...segment.responseWindow, response_id: "different" }, { ...segment.responseWindow, end_ms: 99999 }]) {
    const value = responseTimeline({ ...segment, responseWindow });
    assert.equal(value.duration, null);
    near(value.text, 314.428);
    near(value.audio, 440.638);
  }
  assert.equal(responseTimeline({ ...segment, firstOutputTiming: { ...segment.firstOutputTiming, boundary_source: "connection_restart" } }).duration, null);
  assert.equal(responseTimeline({ ...segment, ttfaMs: 9999 }).duration, null);
});

test("a zero-duration response retains coincident zero measurements without inventing an interval", () => {
  const firstOutputTiming = { start_ms: 0, boundary_ms: 0, boundary_source: "response_stop", text_ttft_ms: 0, ttfa_ms: 0 };
  const responseWindow = { start_ms: 0, end_ms: 0, duration_ms: 0 };
  assert.deepEqual(responseTimeline({ textTtftMs: 0, ttfaMs: 0, firstOutputTiming, responseWindow }), { text: 0, gap: 0, audio: 0, duration: 0 });
});

test("legacy ttft_ms is audio only; old caller-gap aliases are never trusted", () => {
  const [segment] = chatSegments({ conversation_turns: [{ speaker: "assistant", text: "Hello", ttft_ms: 450, caller_reply_gap_ms: 12000, response_wait_ms: 12000, latency_ms: 12000 }] });
  assert.equal(segment.ttfaMs, 450);
  assert.equal(segment.textTtftMs, null);
  assert.equal(segment.callerReplyGapMs, null);
  assert.equal(segment.responseWindow, null);
  const window = normalizeResponseWindow({ start_ms: 0, duration_ms: 1000, ttft_ms: 450 });
  assert.equal(window.first_audio_ms, 450);
  assert.equal(window.first_text_ms, null);
});

test("the per-utterance timeline does not borrow grouped row indexes", () => {
  const record = {
    turns: [
      { index: 0, speaker: "assistant", start_ms: 400, text: "Hello" },
      { index: 1, speaker: "assistant", start_ms: 1400, text: "How can I help?" },
    ],
    conversation_turns: [{ index: 0, speaker: "assistant", start_ms: 400, text: "Hello How can I help?" }],
    nova_response_turns: [
      { index: 0, response_id: "same", start_ms: 0, duration_ms: 500, text_ttft_ms: 100 },
      { index: 1, response_id: "same", start_ms: 1000, duration_ms: 500, text_ttft_ms: 200 },
    ],
  };
  assert.deepEqual(utteranceSegments(record).map((s) => [s.text, s.textTtftMs]), [["Hello", 100], ["How can I help?", 200]]);
  assert.equal(chatSegments(record).length, 1);
});

test("a response with multiple assistant finals supplies first-output metrics once", () => {
  const record = {
    turns: [
      { speaker: "assistant", start_ms: 300, text: "First", text_ttft_ms: 999 },
      { speaker: "assistant", start_ms: 400, text: "Second", text_ttft_ms: 999 },
    ],
    nova_response_turns: [
      { index: 0, start_ms: 0, duration_ms: 1000, text_ttft_ms: 50 },
      { index: 1, start_ms: 100, duration_ms: 800, text_ttft_ms: 100 },
    ],
  };
  const [first, second] = utteranceSegments(record);
  assert.equal(first.responseWindow.response_index, 1);
  assert.equal(first.textTtftMs, 100);
  assert.equal(second.responseWindow, null);
  assert.equal(second.textTtftMs, null);
});

test("equal-start overlaps prefer the most recent response index", () => {
  const [segment] = utteranceSegments({
    turns: [{ speaker: "assistant", start_ms: 300 }],
    nova_response_turns: [{ index: 0, start_ms: 0, duration_ms: 500, text_ttft_ms: 100 }, { index: 1, start_ms: 0, duration_ms: 500, text_ttft_ms: 200 }],
  });
  assert.equal(segment.textTtftMs, 200);
});

test("an unrelated nearby response never supplies timing and tool-only windows do not steal it", () => {
  const record = {
    turns: [{ speaker: "assistant", start_ms: 300 }, { speaker: "assistant", start_ms: 2000, text_ttft_ms: 99 }],
    nova_response_turns: [
      { index: 0, start_ms: 0, duration_ms: 1000, text_ttft_ms: 100 },
      { index: 1, start_ms: 100, duration_ms: 500 },
    ],
  };
  const [first, outside] = utteranceSegments(record);
  assert.equal(first.responseWindow.response_index, 0);
  assert.equal(outside.responseWindow, null);
  assert.equal(outside.textTtftMs, null);
});

test("an explicit null response window is authoritative even with old offsets present", () => {
  const [segment] = utteranceSegments({ turns: [{ speaker: "assistant", start_ms: 300, response_window: null, text_ttft_ms: 99 }] });
  assert.equal(segment.textTtftMs, null);
});

test("audio before caller final remains unknown rather than zero or negative silence", () => {
  const segments = utteranceSegments({
    turns: [{ speaker: "user", start_ms: 300 }, { speaker: "assistant", start_ms: 400 }],
    nova_response_turns: [{ start_ms: 0, duration_ms: 500, ttfa_ms: 200 }],
  });
  assert.equal(segments[1].callerReplyGapMs, null);
});

test("a partial transcript never substitutes for a caller final or authorizes response timing", () => {
  const segments = chatSegments({
    conversation_turns: [
      { speaker: "user", start_ms: 100, end_ms: 100, transcript_final_ms: null, transcript_interval_ms: 300 },
      { speaker: "assistant", start_ms: 400, transcript_final_ms: 400 },
    ],
    nova_response_turns: [{ start_ms: 0, duration_ms: 500, ttfa_ms: 200 }],
  });
  assert.equal(segments[0].transcriptFinalMs, null);
  assert.equal(segments[0].transcriptIntervalMs, null);
  assert.equal(segments[1].callerReplyGapMs, null);
  const [partialAssistant] = utteranceSegments({ turns: [{ speaker: "assistant", start_ms: 300, transcript_final_ms: null }], nova_response_turns: [{ start_ms: 0, duration_ms: 500, text_ttft_ms: 100 }] });
  assert.equal(partialAssistant.responseWindow, null);
});

test("separate timing values preserve missing observations, audio-first order and real zeroes", () => {
  assert.deepEqual(firstAudioValues(200, 400), { text: 400, gap: null, audio: 200 });
  assert.deepEqual(firstAudioValues(200, null), { text: null, gap: null, audio: 200 });
  assert.deepEqual(firstAudioValues(200, 200), { text: 200, gap: 0, audio: 200 });
  assert.deepEqual(firstAudioValues(200, 0), { text: 0, gap: 200, audio: 200 });
  assert.deepEqual(firstAudioValues(0, 0), { text: 0, gap: 0, audio: 0 });
  for (const invalid of [null, undefined, NaN, Infinity, -10]) {
    assert.deepEqual(firstAudioValues(invalid, 100), { text: 100, gap: null, audio: null });
    assert.deepEqual(firstAudioValues(200, invalid), { text: null, gap: null, audio: 200 });
  }
});

test("invalid markers and unfinished response boundaries cannot produce a completed bar", () => {
  assert.equal(normalizeResponseWindow({ start_ms: 100, end_ms: 90 }), null);
  assert.equal(normalizeResponseWindow({ start_ms: NaN, duration_ms: 100 }), null);
  for (const end_source of ["next_response_start", "session_end", "connection_restart"]) {
    assert.equal(normalizeResponseWindow({ start_ms: 0, duration_ms: 1000, end_source, text_ttft_ms: 100 }), null);
  }
  assert.equal(normalizeResponseWindow({ start_ms: 0, duration_ms: 1000, stop_reason: "incomplete", text_ttft_ms: 100 }), null);
  assert.ok(normalizeResponseWindow({ start_ms: 0, duration_ms: 1000, stop_reason: "end_turn", text_ttft_ms: 100 }));
  assert.ok(normalizeResponseWindow({ start_ms: 0, duration_ms: 1000, end_source: "response_stop", stop_reason: "interrupted", text_ttft_ms: 100 }));
  const window = normalizeResponseWindow({ start_ms: 100, duration_ms: 1000, first_text_ms: 50, first_audio_ms: 1200 });
  assert.equal(window.text_ttft_ms, null);
  assert.equal(window.ttfa_ms, null);
});

test("forced closures retain recorded first-output timing without a completed-response bar", () => {
  for (const boundary of [
    { end_source: "next_response_start" },
    { end_source: "session_end" },
    { end_source: "connection_restart" },
    { stop_reason: "next_response_start" },
    { stop_reason: "incomplete" },
    { stop_reason: "connection_restart" },
  ]) {
    const [caller, assistant] = utteranceSegments({
      turns: [{ speaker: "user", start_ms: 100 }, { speaker: "assistant", start_ms: 400 }],
      nova_response_turns: [{ start_ms: 0, duration_ms: 500, text_ttft_ms: 150, ttfa_ms: 200, ...boundary }],
    });
    assert.equal(caller.textTtftMs, null);
    assert.equal(assistant.responseWindow, null);
    assert.equal(assistant.textTtftMs, 150);
    assert.equal(assistant.ttfaMs, 200);
    assert.equal(assistant.callerReplyGapMs, 100);
    assert.deepEqual(firstAudioValues(assistant.ttfaMs, assistant.textTtftMs), { text: 150, gap: 50, audio: 200 });
  }
});

test("the new first-output contract preserves TTFT independently of an explicit null response window", () => {
  const firstOutput = normalizeFirstOutputTiming({ start_ms: 0, duration_ms: 500, text_ttft_ms: 150, ttfa_ms: 200, end_source: "session_end" });
  const [segment] = utteranceSegments({ turns: [{ speaker: "assistant", start_ms: 400, first_output_timing: firstOutput, response_window: null }] });
  assert.equal(segment.textTtftMs, 150);
  assert.equal(segment.ttfaMs, 200);
  assert.equal(segment.responseWindow, null);
  assert.equal(normalizeFirstOutputTiming({ start_ms: 0, duration_ms: 500, text_ttft_ms: 150, end_source: "unknown" }), null);
});

test("a delayed final before all first-output markers does not claim the response", () => {
  const [delayed, actual] = utteranceSegments({
    turns: [{ speaker: "assistant", start_ms: 100 }, { speaker: "assistant", start_ms: 400 }],
    nova_response_turns: [{ start_ms: 0, duration_ms: 500, text_ttft_ms: 150, ttfa_ms: 200 }],
  });
  assert.equal(delayed.textTtftMs, null);
  assert.equal(delayed.responseWindow, null);
  assert.equal(actual.textTtftMs, 150);
  assert.ok(actual.responseWindow);
});

test("raw transcript fallback preserves words and does not invent intervals", () => {
  const segments = chatSegments({ transcripts: [{ role: "user", text: "Partial", t_ms: 5 }, { role: "user", text: "Finished words", t_ms: 10, is_final: true }] });
  assert.equal(segments.length, 1);
  assert.equal(segments[0].text, "Finished words");
  assert.equal(segments[0].transcriptIntervalMs, null);
  assert.equal(segments[0].textTtftMs, null);
});

test("tools match individual response windows once and keep their invocation turn when completion is late", () => {
  const first = { tool: "save", tool_use_id: "a", t_ms: 200, completed_t_ms: 1450, source: "measured_execution", status: "success", kind: "async", execution_mode: "thread_offload" };
  const second = { tool: "save", tool_use_id: "b", t_ms: 700, source: "measured_execution" };
  const third = { tool: "end", tool_use_id: "c", t_ms: 1050 };
  const record = {
    total_e2e_ms: 1550,
    turns: [{ speaker: "user", start_ms: 100 }, { speaker: "assistant", start_ms: 400 }, { speaker: "assistant", start_ms: 900 }, { speaker: "user", start_ms: 1000 }, { speaker: "assistant", start_ms: 1400 }],
    conversation_turns: [{ index: 99, speaker: "assistant", start_ms: 400, tool_calls: [{ ...first, status: "running" }, second, third] }],
    nova_response_turns: [{ start_ms: 150, end_ms: 500, ttfa_ms: 50 }, { start_ms: 600, end_ms: 950, ttfa_ms: 50 }, { start_ms: 1100, end_ms: 1500, ttfa_ms: 50 }],
    tool_executions: [first, second, third, { tool: "late", tool_use_id: "d", t_ms: 1600 }, { tool: "unknown", tool_use_id: "e" }, { tool: "post", tool_use_id: "f", phase: "post_call", t_ms: 1300 }],
    events_log: [{ event_type: "tool_invocation", tool: "save", tool_use_id: "a", t_ms: 199 }],
  };
  const { turns, unassignedTools } = turnMetrics(record);
  assert.deepEqual(turns.map((turn) => turn.toolCalls.map((call) => call.tool_use_id)), [[], ["a"], ["b"], [], ["c"]]);
  assert.equal(turns[1].toolCalls[0].status, "success");
  assert.equal(turns[1].toolCalls[0].association, "response_event_time_estimate");
  assert.equal(turns[4].toolCalls[0].association, "transcript_event_time_estimate");
  assert.deepEqual(unassignedTools.map((call) => call.tool_use_id), ["d", "e"]);
  assert.equal(inCallTools(record).length, 5);
});

test("legacy tool copies are deduplicated without merging repeated jobs or using grouped indexes", () => {
  const jobs = [{ tool: "save", t_ms: 650 }, { tool: "save", t_ms: 650 }];
  const record = {
    turns: [{ speaker: "user", start_ms: 100 }, { speaker: "assistant", start_ms: 400 }, { speaker: "user", start_ms: 600 }, { speaker: "assistant", start_ms: 900 }],
    tool_executions: jobs,
    conversation_turns: [{ index: 0, speaker: "assistant", start_ms: 400, tool_calls: jobs }],
    events_log: jobs.map((job) => ({ ...job, event_type: "tool_invocation" })),
  };
  assert.equal(inCallTools(record).length, 2);
  const { turns, unassignedTools } = turnMetrics(record);
  assert.deepEqual(turns.map((turn) => turn.toolCalls.length), [0, 0, 0, 2]);
  assert.equal(unassignedTools.length, 0);
  assert.equal(turns[3].toolCalls[0].source, "invocation_only");
  assert.equal(turns[3].toolCalls[0].kind, "unknown");
});

test("distinct tool IDs survive equal timestamps and jobs with no assistant remain visible", () => {
  const jobs = [{ tool: "save", tool_use_id: "a", t_ms: 100 }, { tool: "save", tool_use_id: "b", t_ms: 100 }];
  assert.equal(inCallTools({ tool_executions: jobs, conversation_turns: [{ tool_calls: jobs.map(({ tool_use_id, ...rest }) => rest) }] }).length, 2);
  const { turns, unassignedTools } = turnMetrics({ turns: [{ speaker: "user", start_ms: 100 }], tool_executions: jobs });
  assert.equal(turns[0].toolCalls.length, 0);
  assert.equal(unassignedTools.length, 2);
});

test("a legacy measured job is promoted to a copied tool ID without duplicating or losing its outcome", () => {
  const measured = { tool: "save", t_ms: 100, status: "success", source: "measured_execution" };
  const jobs = inCallTools({
    tool_executions: [measured],
    conversation_turns: [{ tool_calls: [{ tool: "save", t_ms: 100, tool_use_id: "u1", status: "running" }, { tool: "save", t_ms: 100, tool_use_id: "u2" }] }],
    events_log: [{ event_type: "tool_invocation", tool: "save", t_ms: 100, tool_use_id: "u1" }],
  });
  assert.equal(jobs.length, 2);
  assert.equal(jobs[0].tool_use_id, "u1");
  assert.equal(jobs[0].status, "success");
  assert.equal(jobs[1].tool_use_id, "u2");
  assert.equal(measured.tool_use_id, undefined);
});

test("Intelligence distinguishes missing history from confirmed absence and uncertain evidence", () => {
  const field = "contextual_data.reason_for_delinquency";
  assert.equal(intelligenceFieldText({}, field), "Not recorded");
  assert.equal(intelligenceFieldText({}, "next_best_action"), "No recommendation recorded");
  assert.equal(intelligenceFieldText({ intelligence_metadata: { field_status: { [field]: "not_discussed" } } }, field), "Not discussed");
  assert.equal(intelligenceFieldText({ intelligence_metadata: { field_status: { [field]: "uncertain" } } }, field), "Not enough evidence");
  assert.equal(intelligenceFieldText({ intelligence_metadata: { field_status: { [field]: "missing" } } }, field), "Not captured");
  assert.equal(intelligenceFieldText({}, field, "Payment delayed by an invoice dispute"), "Payment delayed by an invoice dispute");
});

test("a completed partial analysis stops post-call polling while pending analysis continues", () => {
  for (const status of ["ready", "partial", "failed"]) assert.equal(isIntelligenceTerminal(status), true);
  for (const status of [null, undefined, "pending", "withheld_unconfirmed_termination"]) assert.equal(isIntelligenceTerminal(status), false);
});

test("every rendered table heading has a labelled information control", () => {
  const files = ["../src/App.tsx", "../src/components/DueRecordsTable.tsx", "../src/components/SonicCost.tsx"];
  let headers = 0;
  for (const file of files) {
    const code = readFileSync(new URL(file, import.meta.url), "utf8");
    const ast = ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const visit = (node) => {
      if (ts.isJsxElement(node) && node.openingElement.tagName.getText(ast) === "TableHead") {
        headers++;
        const info = node.children.find((child) => ts.isJsxSelfClosingElement(child) && child.tagName.getText(ast) === "ColumnHeading");
        assert.ok(info, `Missing column help: ${node.getText(ast)}`);
        const attributes = info.attributes.properties.map((p) => p.name?.getText(ast));
        assert.ok(attributes.includes("label") && attributes.includes("help"));
        assert.ok(node.openingElement.attributes.properties.some((p) => p.name?.getText(ast) === "scope"));
      }
      ts.forEachChild(node, visit);
    };
    visit(ast);
  }
  assert.equal(headers, 30);
});
