import {
  create,
  createLlmStepFunction,
  createOpenAIProvider,
  createRuntimeRegistry,
  emptyKnowledge,
  emptyState,
  err,
  freezeContext,
  makeLoomError,
  newContextId,
  newLoopId,
  newLoopVersion,
  newRunId,
  ok,
  run,
  type Context,
  type ISODateTime,
  type JsonValue,
  type MinimalLoopDefinition,
  type Result,
  type RunResult,
  type ToolHandler,
  type ToolRef,
} from "../index.js";

const searchNotesTool: ToolRef = {
  id: "search-notes",
  description: "Search local project notes for relevant Loom context",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string" },
    },
    required: ["query"],
    additionalProperties: false,
  },
};

export interface LlmLoopOptions {
  readonly apiKey: string;
  readonly model?: string;
  readonly baseUrl?: string;
}

export function makeInitialLlmContext(): Context {
  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now(),
    identity: {
      role: "Loom planning agent",
      capabilities: [
        {
          id: "decide-next-action",
          description: "Choose the next loop action from current context",
        },
      ],
      constraints: [
        {
          id: "structured-output",
          description: "Return a valid JSON decision object",
          severity: "must",
        },
      ],
    },
    goal: {
      objective: "Decide the next useful action for the Loom project",
      criteria: [
        {
          id: "decision-recorded",
          description: "A decision is added to context state",
          required: true,
        },
      ],
      budget: {
        maxSteps: 1,
        maxTokens: 4000,
      },
    },
    state: emptyState(),
    knowledge: emptyKnowledge(),
    affordances: {
      tools: [searchNotesTool],
      loops: [],
      resources: [],
    },
  });
}

export function makeLlmLoopDefinition(options: LlmLoopOptions): MinimalLoopDefinition {
  const provider = createOpenAIProvider({
    apiKey: options.apiKey,
    model: options.model === undefined ? "gpt-4o-mini" : options.model,
    ...(options.baseUrl === undefined ? {} : { baseUrl: options.baseUrl }),
  });

  return {
    id: newLoopId(),
    version: newLoopVersion(),
    identity: {
      role: "LLM loop",
      capabilities: [
        {
          id: "llm-step",
          description: "Use an LLM provider to choose the next action",
        },
      ],
      constraints: [],
    },
    goal: {
      objective: "Run an LLM-backed Loom step",
      criteria: [],
      budget: {},
    },
    step: createLlmStepFunction({
      provider,
      enableToolCalling: true,
      maxToolCallsPerStep: 3,
    }),
    done: (context) => ok(context.state.decisions.length > 0),
  };
}

export async function runLlmLoop(options: LlmLoopOptions): Promise<Result<RunResult>> {
  if (options.apiKey.length === 0) {
    return err(
      makeLoomError({
        code: "VALIDATION_FAILED",
        message: "An OpenAI API key is required",
        retryable: false,
      }),
    );
  }

  const toolHandler: ToolHandler = {
    ref: searchNotesTool,
    invoke: (input: JsonValue) =>
      Promise.resolve(
        ok({
          id: "search-notes-observation",
          source: searchNotesTool.id,
          value: {
            input,
            matches: [
              {
                title: "Loom architecture",
                summary: "Use context layers, Result values, and append-only state.",
              },
            ],
          },
          at: now(),
        }),
      ),
  };
  const handle = create(makeLlmLoopDefinition(options), {
    registry: createRuntimeRegistry({
      tools: new Map([[searchNotesTool.id, toolHandler]]),
    }),
  });
  if (!handle.ok) {
    return handle;
  }

  return run(handle.value, makeInitialLlmContext(), { maxSteps: 1 });
}

export function readOpenAIApiKeyFromEnv(): string {
  const globalWithProcess = globalThis as typeof globalThis & {
    readonly process?: {
      readonly env?: Readonly<Record<string, string | undefined>>;
    };
  };
  if (globalWithProcess.process === undefined || globalWithProcess.process.env === undefined) {
    return "";
  }
  const apiKey = globalWithProcess.process.env.OPENAI_API_KEY;
  return apiKey === undefined ? "" : apiKey;
}

function now(): ISODateTime {
  return new Date().toISOString() as ISODateTime;
}
