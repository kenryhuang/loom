"""Task profiles for compiling user objectives into Loom context layers."""

from __future__ import annotations

from dataclasses import dataclass

from loom.core import Result, err, make_loom_error, ok
from loom.tasks.request import TaskRequest


@dataclass(frozen=True, slots=True)
class TaskProfile:
    id: str
    role: str
    constraints: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    blueprint: str = "direct_llm_tool_loop"

    def __post_init__(self) -> None:
        object.__setattr__(self, "constraints", tuple(self.constraints))
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))


GENERAL_PROFILE = TaskProfile(
    id="general",
    role="generic task runner",
    constraints=(
        "Use available tools to gather evidence before making claims.",
        "Prefer small reversible actions unless the user explicitly asks for broader changes.",
        "Call finish with the final report when the task is complete.",
    ),
    expected_outputs=("A concise final report that answers the requested task.",),
)

PROJECT_AUDIT_PROFILE = TaskProfile(
    id="project_audit",
    role="project audit task runner",
    constraints=(
        "Inspect the project structure and primary documentation before judging purpose.",
        "Run relevant smoke or verification commands when the workspace provides them.",
        "Do not modify source files unless the user explicitly asks for edits.",
        "Ground purpose and improvement recommendations in observed files or command output.",
        "Call finish with a markdown audit report when the audit is complete.",
    ),
    expected_outputs=(
        "A markdown report covering project purpose, smoke test result, evidence, risks, and improvement directions.",
    ),
    blueprint="explore_execute_synthesize",
)

_PROFILES = {
    GENERAL_PROFILE.id: GENERAL_PROFILE,
    PROJECT_AUDIT_PROFILE.id: PROJECT_AUDIT_PROFILE,
}


def get_task_profile(profile_id: str) -> Result:
    profile = _PROFILES.get(profile_id)
    if profile is None:
        return err(make_loom_error("VALIDATION_FAILED", "Unknown task profile", retryable=False, metadata={"profile": profile_id}))
    return ok(profile)


def select_task_profile(request: TaskRequest) -> TaskProfile:
    if request.profile != "auto":
        profile = _PROFILES.get(request.profile)
        if profile is not None:
            return profile
        return GENERAL_PROFILE

    objective = request.objective.lower()
    project_markers = ("audit", "smoke", "project", "repo", "repository", "codebase", "improvement")
    if request.workspace is not None and any(marker in objective for marker in project_markers):
        return PROJECT_AUDIT_PROFILE
    return GENERAL_PROFILE


__all__ = [
    "GENERAL_PROFILE",
    "PROJECT_AUDIT_PROFILE",
    "TaskProfile",
    "get_task_profile",
    "select_task_profile",
]
