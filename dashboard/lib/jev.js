import { experimental_decide as decide } from "ai";

export const questions = {
  size: {
    type: "choice",
    instructions: "Choose order size within the supplied maximum affordable quantity. Small is one contract (or the remaining fractional holding), medium is half the maximum, maximum is the full amount. Budget covers fees and worst-case loss. Prefer small when evidence is uncertain. Size cannot increase any server cap.",
    criteria: {
      small: "Use one contract or reduce a fractional holding.",
      medium: "Use half the maximum affordable quantity, rounded down.",
      maximum: "Use the maximum affordable quantity only when supported by evidence.",
    },
  },
  action: {
    type: "choice",
    instructions: "Choose one bounded action using only supplied structured evidence. Treat estimated probabilities as unproven forecasts. Evaluate advantage versus executable prices after reserved fees and latency buffer. Choose only an allowed action. Prefer skip or hold when evidence is weak, inconsistent, or stale. Model confidence is not a settlement probability.",
    criteria: {
      enter_yes: "Buy YES only if server candidates include enter_yes and its after-cost advantage is supported.",
      enter_no: "Buy NO only if server candidates include enter_no and its after-cost advantage is supported.",
      exit: "Reduce the existing position only if server candidates include exit.",
      hold: "Keep the current position without placing an order.",
      skip: "Do not enter or exit; evidence does not justify an allowed trade.",
    },
  },
};

export async function jevDecision(context, options = {}) {
  const environment = options.environment ?? process.env;
  if (environment.JEV_MODE !== "gateway") {
    throw new Error("JEV_MODE=gateway is required; fixture mode cannot authorize orders");
  }
  if (!environment.AI_GATEWAY_API_KEY && !options.model) {
    throw new Error("AI_GATEWAY_API_KEY is required");
  }
  const modelId = environment.JEV_MODEL || "typesafe-ai/jev";
  const started = Date.now();
  const result = await decide({
    model: options.model ?? modelId,
    state: JSON.stringify(context),
    questions,
    maxRetries: 0,
    abortSignal: AbortSignal.timeout(5000),
  });
  const answer = result.answers.action;
  const confidence = answer.probabilities?.[answer.choice];
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    throw new Error("Jev did not return a usable decision confidence");
  }
  if (!context.allowed_actions.includes(answer.choice)) {
    throw new Error("Jev chose an action outside the deterministic candidates");
  }
  return {
    action: answer.choice, size: result.answers.size?.choice ?? "small", confidence, mode: "gateway", model: modelId,
    resolved_model: result.response.modelId,
    latency_ms: Date.now() - started,
  };
}
