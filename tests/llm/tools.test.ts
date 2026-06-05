import { describe, expect, it } from "vitest";

import { toLlmTool, toLlmTools, type ToolRef } from "../../src/index.js";

describe("LLM tool conversion", () => {
  it("converts ToolRef values to OpenAI-compatible function tools", () => {
    const schema = {
      type: "object",
      properties: {
        query: { type: "string" },
      },
      required: ["query"],
    } as const;
    const tool: ToolRef = {
      id: "search",
      description: "Search indexed notes",
      inputSchema: schema,
    };

    expect(toLlmTool(tool)).toEqual({
      type: "function",
      function: {
        name: "search",
        description: "Search indexed notes",
        parameters: schema,
      },
    });
  });

  it("uses an open object schema when a tool has no input schema", () => {
    const tool = toLlmTool({
      id: "clock",
      description: "Read the current time",
    });

    expect(tool.function.parameters).toEqual({
      type: "object",
      properties: {},
      additionalProperties: true,
    });
  });

  it("converts tool arrays without mutating them", () => {
    const tools: readonly ToolRef[] = [
      {
        id: "search",
        description: "Search indexed notes",
      },
      {
        id: "summarize",
        description: "Summarize a document",
      },
    ];

    expect(toLlmTools(tools).map((tool) => tool.function.name)).toEqual([
      "search",
      "summarize",
    ]);
    expect(tools).toHaveLength(2);
  });
});
