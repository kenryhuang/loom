"""Persistent acceptance summary shared by one-shot and session terminals."""

from rich.text import Text
from textual.widgets import Static

LABELS = {"not_ready": "Ready to work", "verifying": "Verifying", "passed": "Passed",
          "needs_repair": "Needs repair", "blocked": "Blocked"}


ROUND_EVENTS = {"acceptance.verifying", "verification.started", "verification.completed", "acceptance.gate.passed", "acceptance.gate.blocked"}


def round_summary(value):
    criteria = (value.get("plan") or {}).get("criteria", [])
    results = {r["criterion_id"]: r for r in value.get("results", [])}
    passed = sum(results.get(c["id"], {}).get("status") == "passed" for c in criteria)
    failed = value["state"] in {"blocked", "needs_repair"} or any(r["status"] in {"failed", "blocked"} for r in results.values())
    title = "Acceptance passed" if value["state"] == "passed" else "Acceptance needs attention" if failed else "Verifying acceptance"
    return f"{title} · {passed}/{len(criteria)} checks passed · Round {value.get('attempts', 0)}", failed


def round_details(value):
    results = {r["criterion_id"]: r for r in value.get("results", [])}
    lines = [value.get("reason", "")]
    for criterion in (value.get("plan") or {}).get("criteria", []):
        result = results.get(criterion["id"], {})
        lines.append(f"[{result.get('status', 'pending')}] {criterion['description']}")
        if result.get("reason"):
            lines.append("  " + result["reason"])
        if result.get("evidence_ids"):
            lines.append("  Evidence: " + ", ".join(result["evidence_ids"]))
        if result.get("artifact", {}).get("sha256"):
            lines.append("  Evidence artifact: " + result["artifact"]["sha256"])
    for requirement in (value.get("plan") or {}).get("unresolved_requirements", []):
        lines.append("[unresolved] " + requirement)
    return "\n".join(lines)


class AcceptancePanel(Static):
    DEFAULT_CSS = """
    AcceptancePanel {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        padding: 0 1;
        background: $surface;
        border-left: solid $accent;
    }
    """

    def __init__(self, *, show_pending=True, **kwargs):
        super().__init__(Text("Acceptance · Automatic · Prepared when the task starts"), **kwargs)
        self.show_pending = show_pending
        self.display = show_pending
        self.run_id = None
        self.acceptance = None
        self.current_check = None

    def restore(self, snapshot):
        self.run_id = (snapshot.get("run") or {}).get("id")
        current = self.run_id and snapshot.get("acceptance_run_id") == self.run_id
        self.acceptance = snapshot.get("acceptance") if current else None
        self.current_check = snapshot.get("acceptance_current_check") if current else None
        self.display = self.show_pending or bool(self.acceptance)
        self.refresh_summary()

    def apply_event(self, event):
        if event.run_id and self.run_id != event.run_id:
            self.run_id = event.run_id
            self.acceptance = self.current_check = None
        data = event.data
        if data.get("acceptance") is not None:
            self.acceptance = data["acceptance"]
        if event.event_type == "verification.started":
            self.current_check = data.get("description")
        elif event.event_type in {"verification.completed", "acceptance.gate.passed", "acceptance.gate.blocked", "acceptance.invalidated"}:
            self.current_check = None
        self.display = self.show_pending or bool(self.acceptance)
        self.refresh_summary()

    def refresh_summary(self):
        value = self.acceptance
        if not value:
            self.update(Text("Acceptance · Automatic · Plan prepared before execution; checks run before completion"))
            return
        criteria = (value.get("plan") or {}).get("criteria", [])
        results = {r["criterion_id"]: r for r in value.get("results", [])}
        passed = sum(results.get(c["id"], {}).get("status") == "passed" for c in criteria)
        label = "Preparing plan" if value["state"] == "not_ready" and not value.get("plan") else LABELS.get(value["state"], value["state"])
        progress = f"{passed}/{len(criteria)} checks passed" if criteria else "Criteria pending"
        lines = [f"Acceptance · {label} · {progress} · revision {value.get('revision', 0)}"]
        if self.current_check:
            lines.append("Checking: " + self.current_check)
        elif value.get("reason"):
            lines.append(value["reason"])
        for criterion in criteria:
            result = results.get(criterion["id"], {})
            lines.append(f"[{result.get('status', 'pending')}] {criterion['description']}")
            if result.get("reason") and result.get("status") in {"failed", "blocked", "stale"}:
                lines.append("  " + result["reason"])
        for requirement in (value.get("plan") or {}).get("unresolved_requirements", []):
            lines.append("[unresolved] " + requirement)
        self.update(Text("\n".join(lines)))
