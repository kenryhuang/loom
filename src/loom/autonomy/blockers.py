"""Typed blockers and recovery policy. Messages never select a transition."""

from enum import StrEnum

from loom.service.contracts import ServiceError


class Code(StrEnum):
    SOURCE_CHANGED = "SOURCE_CHANGED"
    ROLE_CHANGED = "ROLE_CHANGED"
    GOAL_TERMINAL = "GOAL_TERMINAL"
    GOAL_MISSING = "GOAL_MISSING"
    BASELINE_MISSING = "BASELINE_MISSING"
    NO_PROGRESS = "NO_PROGRESS"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    REVIEW_GAP = "REVIEW_GAP"
    PLANNING_BUDGET = "PLANNING_BUDGET"
    EXECUTION_BUDGET = "EXECUTION_BUDGET"
    MODEL_PROTOCOL = "MODEL_PROTOCOL"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    UNKNOWN_RESPONSE = "UNKNOWN_RESPONSE"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    LEGACY_UNCLASSIFIED = "LEGACY_UNCLASSIFIED"
    SESSION_OWNERSHIP = "SESSION_OWNERSHIP"
    EXECUTION_BUSY = "EXECUTION_BUSY"
    EXECUTION_INPUT = "EXECUTION_INPUT"
    EXECUTION_CONTRACT = "EXECUTION_CONTRACT"
    EXECUTION_CHECKPOINT = "EXECUTION_CHECKPOINT"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    MANUAL_PAUSE = "MANUAL_PAUSE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    LEARNING_DEPENDENCY = "LEARNING_DEPENDENCY"
    GOAL_STATE = "GOAL_STATE"
    STAGE_STATE = "STAGE_STATE"
    DEPENDENCY_WAIT = "DEPENDENCY_WAIT"
    RETRY_BACKOFF = "RETRY_BACKOFF"
    WORK_EXISTS = "WORK_EXISTS"


class Recovery(StrEnum):
    REVISE_CONTRACT = "revise_contract"
    CHANGE_INPUT = "change_input"
    ANSWER = "answer"
    WAIT_BUDGET = "wait_budget"
    ADJUST_BUDGET = "adjust_budget"
    MANUAL_RETRY = "manual_retry"
    RECONCILE = "reconcile"


POLICY = {
    Code.SOURCE_CHANGED: Recovery.REVISE_CONTRACT,
    Code.ROLE_CHANGED: Recovery.REVISE_CONTRACT,
    Code.GOAL_TERMINAL: Recovery.CHANGE_INPUT,
    Code.GOAL_MISSING: Recovery.CHANGE_INPUT,
    Code.BASELINE_MISSING: Recovery.CHANGE_INPUT,
    Code.NO_PROGRESS: Recovery.CHANGE_INPUT,
    Code.INPUT_REQUIRED: Recovery.ANSWER,
    Code.REVIEW_GAP: Recovery.CHANGE_INPUT,
    Code.PLANNING_BUDGET: Recovery.WAIT_BUDGET,
    Code.EXECUTION_BUDGET: Recovery.WAIT_BUDGET,
    Code.MODEL_PROTOCOL: Recovery.MANUAL_RETRY,
    Code.CONTRACT_INVALID: Recovery.MANUAL_RETRY,
    Code.UNKNOWN_RESPONSE: Recovery.CHANGE_INPUT,
    Code.RUNTIME_FAILURE: Recovery.MANUAL_RETRY,
    Code.LEGACY_UNCLASSIFIED: Recovery.MANUAL_RETRY,
    Code.SESSION_OWNERSHIP: Recovery.CHANGE_INPUT,
    Code.EXECUTION_BUSY: Recovery.RECONCILE,
    Code.EXECUTION_INPUT: Recovery.ANSWER,
    Code.EXECUTION_CONTRACT: Recovery.REVISE_CONTRACT,
    Code.EXECUTION_CHECKPOINT: Recovery.CHANGE_INPUT,
    Code.EXECUTION_FAILURE: Recovery.MANUAL_RETRY,
    Code.RECOVERY_EXHAUSTED: Recovery.CHANGE_INPUT,
    Code.MANUAL_PAUSE: Recovery.CHANGE_INPUT,
    Code.SOURCE_UNAVAILABLE: Recovery.CHANGE_INPUT,
    Code.LEARNING_DEPENDENCY: Recovery.RECONCILE,
    Code.GOAL_STATE: Recovery.CHANGE_INPUT,
    Code.STAGE_STATE: Recovery.CHANGE_INPUT,
    Code.DEPENDENCY_WAIT: Recovery.RECONCILE,
    Code.RETRY_BACKOFF: Recovery.RECONCILE,
    Code.WORK_EXISTS: Recovery.RECONCILE,
}
RECONCILED = {Code.SOURCE_CHANGED, Code.ROLE_CHANGED, Code.GOAL_TERMINAL, Code.GOAL_MISSING}


def make(code, scope_id, message, *, dependency_key=None, details=None, phase="stage_planning"):
    code = Code(code)
    recovery = POLICY[code]
    if code in {Code.PLANNING_BUDGET, Code.EXECUTION_BUDGET}:
        scopes = {s["scope"] for s in (details or {}).get("scopes", [])}
        recovery = Recovery.WAIT_BUDGET if scopes and scopes <= {"daily", "calls", "learning"} else Recovery.ADJUST_BUDGET
    return dict(schema_version=1, code=code.value, scope_id=scope_id, phase=phase,
                recovery=recovery.value, dependency_key=dependency_key, details=details or {}, message=message)


def manual_required(blocker):
    if not blocker:
        return False
    return blocker["recovery"] not in {Recovery.WAIT_BUDGET, Recovery.RECONCILE}


def retryable(blocker):
    return bool(blocker and blocker["recovery"] == Recovery.MANUAL_RETRY)


def from_exception(exc, scope_id, fingerprint, budget_key):
    from loom.llm.structured import ProtocolExhausted

    if isinstance(exc, ServiceError) and exc.code == "AGENT_BUDGET":
        return make(Code.PLANNING_BUDGET, scope_id, str(exc), dependency_key=budget_key,
                    details=getattr(exc, "budget_block", {}))
    if isinstance(exc, ProtocolExhausted):
        return make(Code.MODEL_PROTOCOL, scope_id, str(exc), dependency_key=fingerprint,
                    details=dict(protocol_code=exc.code, issues=exc.issues))
    if isinstance(exc, ServiceError) and exc.code in POLICY:
        return make(exc.code, scope_id, str(exc), dependency_key=fingerprint, details=getattr(exc, "details", {}), phase="execution")
    if isinstance(exc, ServiceError):
        return make(Code.CONTRACT_INVALID, scope_id, str(exc), dependency_key=fingerprint,
                    details=dict(error_code=exc.code, **getattr(exc, "details", {})))
    return make(Code.RUNTIME_FAILURE, scope_id, str(exc), dependency_key=fingerprint,
                details=dict(exception_type=type(exc).__name__))


def holds(blocker, fingerprint, budget_key, *, retry_requested=False):
    if not blocker or blocker["code"] in RECONCILED:
        return False  # Structural predicates are re-evaluated by the coordinator.
    if retry_requested and retryable(blocker):
        return False
    if blocker["recovery"] == Recovery.ANSWER or blocker["code"] == Code.UNKNOWN_RESPONSE:
        return True
    current = budget_key if blocker["code"] == Code.PLANNING_BUDGET else fingerprint
    return blocker.get("dependency_key") in {None, current}


def instructions(blocker):
    return [{
        Recovery.REVISE_CONTRACT: "复核变化的来源或合同，修订后再调度。",
        Recovery.CHANGE_INPUT: "查看阻塞详情，补充条件或处理失败项后继续。",
        Recovery.ANSWER: "回答当前待处理问题，或确认执行结果。",
        Recovery.WAIT_BUDGET: "等待预算窗口恢复后自动复查。",
        Recovery.ADJUST_BUDGET: "调整对应预算额度后再调度，累计用量会保留。",
        Recovery.MANUAL_RETRY: "检查错误并修正配置或输入后，点击手动调度重试一次。",
        Recovery.RECONCILE: "等待依赖条件满足后自动复查。",
    }[blocker["recovery"]]]
