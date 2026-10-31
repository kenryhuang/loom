"""Deterministic clock, failure points and shared autonomous-work fixtures."""

from datetime import UTC, datetime, timedelta


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Faults:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []

    def __call__(self, point):
        self.calls.append(point)
        if point == self.fail_at:
            self.fail_at = None
            raise RuntimeError(f"Injected failure: {point}")


def role(workspace):
    return {
        "name": "研发搭档",
        "role_text": "维护设计质量，中文输出，先查实现再更新设计。",
        "workspace": str(workspace),
        "long_term_objectives": [{"id": "quality", "desired_outcome": "设计与实现一致", "mode": "ongoing"}],
        "source_routes": [{"id": "code", "topic": "实现事实", "paths": ["src", "tests"]}],
        "methods": ["结论附代码依据"],
    }


def goals():
    return [
        {"objective": "核对 Session 的实现边界", "acceptance": ["交付实现依据"], "long_term_objective_id": "quality"},
        {"objective": "核对 Session 的设计边界", "acceptance": ["区分实现与提案"], "long_term_objective_id": "quality"},
    ]


def single_package(agent, goal, context):
    return {"work_packages": [{"objective": goal["objective"], "acceptance": goal["acceptance"], "reason": "Explicit user goal"}]}


def evaluation_provider(_model):
    return LearningJudge()


class LearningJudge:
    model = "fixture-learning"

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        import json

        from loom.core import ok
        from loom.llm import LlmResponse, TokenUsage
        from tests.evaluation.test_incremental_graph import GraphJudge

        payload = json.loads(messages[1].content)
        if payload.get("mode") not in {"extract", "synthesis"}:
            return await GraphJudge().chat(messages, tools, cancellation, **kwargs)
        result = dict(contributions=[], facts=[], lessons=[], workflows=[], evidence_requests=[])
        if payload["mode"] == "extract":
            result["contributions"] = [
                dict(
                    key="contribution-" + s["id"],
                    stage_id=s["id"],
                    rating="unknown",
                    rationale="Fixture does not infer contribution",
                    key_action_ids=[],
                    evidence=[],
                )
                for s in payload["stages"]
            ]
            evidence = payload.get("evidence", [e for a in payload["actions"] for e in a.get("evidence", [])])
            entry = next((e for e in evidence if e.get("basis") == "observed_output" and "Session fixture evidence" in e.get("text", "")), None)
            if entry:
                result["facts"] = [
                    dict(
                        key="session-fact",
                        summary="Session fixture evidence is available in notes.txt",
                        scope="This workspace and recorded run",
                        confidence="medium",
                        category="project",
                        basis="observed",
                        revalidate_when="Before relying on current file content",
                        evidence=[entry["id"]],
                    )
                ]
            failure = next((e for e in evidence if e.get("basis") == "observed_output" and "missing-fixture.txt" in e.get("text", "")), None)
            if failure:
                result["lessons"] = [
                    dict(
                        key="missing-file-lesson",
                        summary="Session missing-file attempt failed; read notes.txt after checking the source route",
                        scope="This fixture workspace",
                        confidence="medium",
                        trigger="Reading missing-fixture.txt",
                        failed_approach="Assume the file exists",
                        failure_reason="Observed file read failure",
                        better_approach="Read the available notes.txt source",
                        validation="proposed",
                        evidence=[failure["id"]],
                    )
                ]
        return ok(LlmResponse(json.dumps(result), usage=TokenUsage(50, 50, 100)))


def solver_provider(state):
    return EvidenceSolver(state)


class EvidenceSolver:
    from tests.acceptance_fakes import FixtureVerifier

    verification_provider = FixtureVerifier()
    model = "fixture-solver"

    def __init__(self, state):
        self.state = state

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        import json

        from loom.core import ok
        from loom.llm import LlmResponse, LlmToolCall, TokenUsage

        names = {t["function"]["name"] for t in tools or []}
        if "continue_react" in names:
            call = LlmToolCall("route", "continue_react", '{"reason":"Inspect fixture evidence"}')
        elif not getattr(self, "read_sent", False):
            self.read_sent = True
            call = LlmToolCall("read", "read_file", '{"path":"notes.txt"}')
        elif "finish" in names:
            call = LlmToolCall("finish", "finish", json.dumps({"report": "Session fixture evidence inspected. Design and implementation distinguished."}))
        else:
            return ok(
                LlmResponse(json.dumps({"action": {"kind": "none", "input": {"report": "Session fixture evidence inspected"}}}), usage=TokenUsage(10, 10, 20))
            )
        return ok(LlmResponse("", (call,), usage=TokenUsage(10, 10, 20)))


def gated_solver_provider(state):
    return GatedSolver(state)


class GatedSolver(EvidenceSolver):
    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        import asyncio
        import json
        from pathlib import Path

        from loom.core import ok
        from loom.llm import LlmResponse, LlmToolCall, TokenUsage

        gated = "boundary-gate" in self.state["task"]["objective"]
        if gated and not getattr(self, "gate_passed", False):
            self.gate_passed = True
            path = Path(self.state["task"]["workspace"])
            (path / "request-pending").touch()
            for _ in range(200):
                if (path / "release-request").exists():
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Test did not release model request")
        if gated and getattr(self, "read_sent", False) and not getattr(self, "recorded", False):
            self.recorded = True
            block = next(m.content for m in messages if m.name == "agent_context")
            assert "Use the corrected scope" in block
            # The host checks this model-reported association against a real call.
            snapshot = json.loads(block[block.index("{") :])
            method = snapshot["methods"][0]["id"]
            call = LlmToolCall(
                "use",
                "agent_record_use",
                json.dumps(dict(method_version_id=method, tool_call_ids=["read"], explanation="Consulted user method while reading fixture evidence")),
            )
            return ok(LlmResponse("", (call,), usage=TokenUsage(10, 10, 20)))
        return await super().chat(messages, tools, cancellation, tool_choice)


def failed_solver_provider(state):
    return FailedEvidenceSolver(state)


class FailedEvidenceSolver(EvidenceSolver):
    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        from loom.core import err, make_loom_error, ok
        from loom.llm import LlmResponse, LlmToolCall, TokenUsage

        if "failure-fixture" not in self.state["task"]["objective"]:
            return await super().chat(messages, tools, cancellation, tool_choice)
        if "continue_react" in {t["function"]["name"] for t in tools or []}:
            return await super().chat(messages, tools, cancellation, tool_choice)
        if not getattr(self, "tried_missing_file", False):
            self.tried_missing_file = True
            return ok(LlmResponse("", (LlmToolCall("missing-read", "read_file", '{"path":"missing-fixture.txt"}'),), usage=TokenUsage(10, 10, 20)))
        return err(make_loom_error("FIXTURE_FAILURE", "Stop after observed missing-file failure", retryable=False))
