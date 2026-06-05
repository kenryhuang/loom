import { afterEach, describe, expect, it, vi } from "vitest";

import { createOpenAIProvider } from "../../src/index.js";

describe("OpenAI provider", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends chat completion requests and parses content, tool calls, and usage", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          choices: [
            {
              finish_reason: "tool_calls",
              message: {
                content: null,
                tool_calls: [
                  {
                    id: "call_1",
                    type: "function",
                    function: {
                      name: "search",
                      arguments: "{\"query\":\"loom\"}",
                    },
                  },
                ],
              },
            },
          ],
          usage: {
            prompt_tokens: 11,
            completion_tokens: 7,
            total_tokens: 18,
          },
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const provider = createOpenAIProvider({
      apiKey: "test-key",
      model: "gpt-test",
      temperature: 0.2,
      maxTokens: 256,
      baseUrl: "https://proxy.example/v1/",
    });

    const result = await provider.chat(
      [
        { role: "system", content: "system" },
        { role: "user", content: "user" },
      ],
      [
        {
          type: "function",
          function: {
            name: "search",
            description: "Search",
            parameters: { type: "object", properties: {} },
          },
        },
      ],
    );

    expect(result.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://proxy.example/v1/chat/completions");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-key",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toMatchObject({
      model: "gpt-test",
      temperature: 0.2,
      max_tokens: 256,
      tools: [
        {
          type: "function",
          function: { name: "search" },
        },
      ],
    });
    if (result.ok) {
      expect(result.value.content).toBeNull();
      expect(result.value.toolCalls).toEqual([
        {
          id: "call_1",
          name: "search",
          arguments: "{\"query\":\"loom\"}",
        },
      ]);
      expect(result.value.usage.totalTokens).toBe(18);
      expect(result.value.finishReason).toBe("tool_calls");
    }
  });

  it("maps HTTP errors to LLM_FAILED LoomError values", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              message: "Incorrect API key provided",
            },
          }),
          { status: 401, statusText: "Unauthorized" },
        ),
      ),
    );
    const provider = createOpenAIProvider({ apiKey: "bad", model: "gpt-test" });

    const result = await provider.chat([{ role: "user", content: "hello" }]);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("LLM_FAILED");
      expect(result.error.retryable).toBe(false);
      expect(result.error.message).toContain("Incorrect API key");
    }
  });

  it("marks rate limits and network errors as retryable LLM failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              message: "Too many requests",
            },
          }),
          { status: 429, statusText: "Too Many Requests" },
        ),
      ),
    );
    const limited = await createOpenAIProvider({
      apiKey: "key",
      model: "gpt-test",
    }).chat([{ role: "user", content: "hello" }]);

    expect(limited.ok).toBe(false);
    if (!limited.ok) {
      expect(limited.error.code).toBe("LLM_FAILED");
      expect(limited.error.retryable).toBe(true);
    }

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    const network = await createOpenAIProvider({
      apiKey: "key",
      model: "gpt-test",
    }).chat([{ role: "user", content: "hello" }]);

    expect(network.ok).toBe(false);
    if (!network.ok) {
      expect(network.error.code).toBe("LLM_FAILED");
      expect(network.error.retryable).toBe(true);
      expect(network.error.message).toContain("offline");
    }
  });
});
