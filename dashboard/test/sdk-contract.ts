import { experimental_decide, type Experimental_DecisionQuestion } from "ai";
import { Experimental_DecisionMockModelV4 } from "ai/test";

const questions = {
  action: {
    type: "choice",
    instructions: "Choose a bounded action.",
    criteria: { enter_yes: "Valid entry", skip: "No valid entry" },
  },
} as const satisfies Record<string, Experimental_DecisionQuestion>;

const model = new Experimental_DecisionMockModelV4({
  doDecide: async () => ({
    answers: { action: { type: "choice", choice: "skip", probabilities: { enter_yes: 0, skip: 1 } } },
    warnings: [],
  }),
});
const result = await experimental_decide({ model, state: {}, questions, maxRetries: 0 });
const choice: "enter_yes" | "skip" = result.answers.action.choice;
const confidence: number | undefined = result.answers.action.probabilities?.[choice];
void confidence;
