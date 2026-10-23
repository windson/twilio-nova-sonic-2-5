import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/sonic-cost.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { sonicCostView, sonicTokens, sonicUsd } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const completed = { total_e2e_ms: 60000, termination: { telephone_completion_confirmed: true } };
const full = { status: "estimated", usage_complete: true, total_cost_usd: "0.012345678", observed_cost_usd: "0.012345678", input_tokens: 1250, output_tokens: 450 };

test("complete costing displays the recorded list estimate without reducing small costs to zero", () => {
  const view = sonicCostView({ ...completed, sonic_cost: full });
  assert.equal(view.state, "estimated");
  assert.equal(view.label, "List-price estimate");
  assert.equal(view.amount, "$0.012345678");
  assert.equal(sonicUsd("0.000000319"), "$0.000000319");
  assert.equal(sonicUsd("0.0000000001"), "<$0.000000001");
  assert.equal(sonicUsd("0"), "$0.00");
  assert.equal(sonicTokens(1250), "1,250");
});

test("costs are withheld while the phone is active or disconnection remains unconfirmed", () => {
  for (const [record, expected] of [
    [{ sonic_cost: full }, "waiting"],
    [{ ...completed, termination: { telephone_completion_confirmed: false }, sonic_cost: full }, "unavailable"],
  ]) {
    const view = sonicCostView(record);
    assert.equal(view.state, expected);
    assert.equal(view.showUsage, false);
    assert.equal(view.amount, "–");
  }
});

test("verified legacy costs remain visible when call duration was not recorded", () => {
  for (const proof of [
    { termination: { telephone_completion_confirmed: true } },
    { twilio_insights: { source: "twilio_call_resource", fields: { status: "completed" } } },
  ]) {
    const view = sonicCostView({ total_e2e_ms: null, ...proof, sonic_cost: full });
    assert.equal(view.state, "estimated");
    assert.equal(view.amount, "$0.012345678");
  }
});

test("unverified or nonterminal Twilio facts do not authorize costs without duration", () => {
  for (const [source, status] of [["assumed", "completed"], ["twilio_call_resource", "in-progress"]]) {
    const view = sonicCostView({ sonic_cost: full, twilio_insights: { source, fields: { status } } });
    assert.equal(view.state, "waiting");
    assert.equal(view.amount, "–");
  }
});

test("partial usage is a subtotal even if a stale call total is present", () => {
  const view = sonicCostView({ ...completed, sonic_cost: {
    ...full, status: "partial", usage_complete: false, observed_cost_usd: "0.0042", total_cost_usd: "999",
  } });
  assert.equal(view.state, "partial");
  assert.equal(view.label, "Observed usage only");
  assert.equal(view.amount, "$0.0042");
  assert.match(view.explanation, /not the full call cost/);
  assert.equal(sonicCostView({ ...completed, sonic_cost: { ...full, usage_complete: false } }).state, "unavailable");
});

test("legacy or unpriced data is not presented as a zero-dollar call", () => {
  assert.equal(sonicCostView(completed).amount, "–");
  assert.equal(sonicCostView(completed).label, "Not calculated yet");
  for (const reason of ["region_not_priced", "modality_counts_missing", "usage_totals_mismatch", "new_reason"]) {
    const view = sonicCostView({ ...completed, sonic_cost: { status: "unavailable", reason, total_cost_usd: "0" } });
    assert.equal(view.state, "unavailable");
    assert.equal(view.amount, "–");
    assert.ok(view.explanation.length > 0);
  }
  for (const value of [undefined, null, "", " ", "NaN", "Infinity", "-1"]) assert.equal(sonicUsd(value), "–");
  for (const value of [undefined, null, -1, 1.5, Infinity, "12"]) assert.equal(sonicTokens(value), "–");
});
