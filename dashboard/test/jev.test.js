import assert from "node:assert/strict";
import test from "node:test";
import { Experimental_DecisionMockModelV4 } from "ai/test";
import { jevDecision } from "../lib/jev.js";

const probabilities = { enter_yes: 0.9, enter_no: 0.02, exit: 0.01, hold: 0.04, skip: 0.03 };
function model(answer, observe = () => {}) {
  return new Experimental_DecisionMockModelV4({
    doDecide: async (input) => {
      observe(input);
      return { answers: { action: answer }, warnings: [], usage: { inputTokens: 10 } };
    },
  });
}
const environment = { JEV_MODE: "gateway" };
const context = { allowed_actions: ["enter_yes", "hold", "skip"], candidates: { enter_yes: {} } };

test("installed SDK decision contract returns answers.action.choice and distribution", async () => {
  let seen;
  const result = await jevDecision(context, {
    environment,
    model: model({ type: "choice", choice: "enter_yes", probabilities }, (input) => { seen = input; }),
  });
  assert.equal(result.action, "enter_yes");
  assert.equal(result.confidence, 0.9);
  assert.equal(result.model, "typesafe-ai/jev");
  assert.equal(result.mode, "gateway");
  assert.deepEqual(JSON.parse(seen.state), context);
  assert.deepEqual(Object.keys(seen.questions.action.criteria),
    ["enter_yes", "enter_no", "exit", "hold", "skip"]);
  assert.match(seen.questions.action.instructions, /not a settlement probability/);
});

test("configuration and malformed model answers fail closed", async () => {
  await assert.rejects(jevDecision(context, { environment: {} }), /JEV_MODE/);
  await assert.rejects(jevDecision(context, { environment }), /AI_GATEWAY_API_KEY/);
  for (const answer of [
    { type: "choice", choice: "enter_yes" },
    { type: "choice", choice: "enter_no", probabilities: {
      enter_yes: 0.02, enter_no: 0.9, exit: 0.01, hold: 0.04, skip: 0.03,
    } },
    { type: "choice", choice: "enter_yes", probabilities: { ...probabilities, enter_yes: NaN } },
    { type: "choice", choice: "buy" },
  ]) {
    await assert.rejects(jevDecision(context, { environment, model: model(answer) }));
  }
});

test("provider failures propagate without an approval fallback", async () => {
  let calls = 0;
  const failed = new Experimental_DecisionMockModelV4({
    doDecide: async () => { calls++; throw new Error("fixture unavailable"); },
  });
  await assert.rejects(jevDecision(context, { environment, model: failed }), /fixture unavailable/);
  assert.equal(calls, 1);
});
