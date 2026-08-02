# Streaming Workflow Route Tool Choice Design

## Problem

Auto plan mode performs an initial workflow-routing LLM call with only
`enter_plan` and `continue_react` visible. The call currently sends
`tool_choice="required"`. DashScope thinking/reasoning models reject that value
with HTTP 400. In streaming mode, the urllib transport also loses the response
body, so Loom sees only `HTTP Error 400: Bad Request` and cannot apply its
existing tool-choice compatibility fallback.

## Design

Initial and review workflow-routing calls will use `tool_choice="auto"` instead
of `required`. The routing contract remains strict at the Loom loop layer:
`require_tool_call` stays enabled, only the two routing tools are visible, and a
missing or invalid routing call receives the existing bounded retry. Therefore
provider compatibility changes without allowing task tools to execute before a
route is selected.

The OpenAI-compatible streaming transport will preserve structured HTTP failure
information. A streaming HTTP error must retain its status and parsed response
body (or readable fallback text), and the resulting Loom error message must
include the provider's diagnostic. This allows the existing compatibility
matcher to recognize unsupported `tool_choice` errors and retry without that
parameter when another constrained workflow uses it.

## Error Handling

- A routing response without a valid route remains bounded by the existing
  missing/invalid tool-call retry policy and ends as `WORKFLOW_ROUTE_FAILED`.
- A provider rejection that explicitly identifies `tool_choice` remains eligible
  for one compatibility retry without the option.
- Other HTTP 4xx failures retain their provider status/body and fail normally;
  they are not silently retried as tool-choice incompatibilities.
- API credentials must never appear in errors, traces, or tests.

## Tests

Tests will be written before implementation and will prove that:

1. Auto workflow routing requests `tool_choice="auto"` while retaining the
   strict Loom-level routing policy.
2. A missing route tool call still triggers the bounded retry and cannot advance
   into task execution.
3. A streaming HTTP 400 preserves the provider diagnostic.
4. A streaming provider rejection mentioning unsupported `tool_choice` triggers
   the compatibility retry and succeeds without disabling streaming.
5. Existing planning, LLM, and task-runner tests remain green, followed by the
   live streaming Auto routing probe against the configured DashScope model.

## Scope

The change is limited to workflow-routing policy and the OpenAI-compatible
streaming error boundary. It does not change normal task tool selection,
planning state transitions, TUI behavior, provider credentials, or user-facing
CLI options.
