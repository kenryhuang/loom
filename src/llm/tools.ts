import type { ToolRef } from "../core/context.js";
import type { JsonValue } from "../core/json.js";
import type { LlmTool } from "./provider.js";

const defaultParameters: JsonValue = {
  type: "object",
  properties: {},
  additionalProperties: true,
};

export function toLlmTool(tool: ToolRef): LlmTool {
  return {
    type: "function",
    function: {
      name: tool.id,
      description: tool.description,
      parameters: tool.inputSchema === undefined ? defaultParameters : tool.inputSchema,
    },
  };
}

export function toLlmTools(tools: readonly ToolRef[]): readonly LlmTool[] {
  return tools.map(toLlmTool);
}
