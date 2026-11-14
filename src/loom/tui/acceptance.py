"""Persistent acceptance summary shared by one-shot and session terminals."""

from rich.text import Text
from textual.widgets import Static

LABELS = {"not_ready": "Ready to work", "verifying": "Verifying", "passed": "Passed",
          "needs_repair": "Needs repair", "blocked": "Blocked"}


ROUND_EVENTS = {"acceptance.verifying", "verification.started", "verification.completed", "acceptance.gate.passed", "acceptance.gate.blocked",
                "visual.capture.started", "visual.capture.completed", "visual.capture.failed", "acceptance.budget.renewed",
                "verification.batch.started", "verification.batch.completed", "verification.pages.planned",
                "verification.selection.started", "verification.selection.completed"}


def progress_text(value):
    if value.get('state') == 'passed':
        return 'All required checks and goal review passed'
    current = (value.get('verification_progress') or {}).get('current') or {}
    stage = current.get('stage')
    ids = current.get('criterion_ids', [])
    names = {c['id']: c['description'] for c in (value.get('plan') or {}).get('criteria', [])}
    target = '; '.join('Overall goal' if i == 'goal' else names.get(i, i) for i in ids)
    label = {'select_evidence': 'Selecting evidence', 'verify_page': 'Inspecting evidence',
             'verify': 'Checking goal coverage and consistency' if 'goal' in ids else 'Judging requirements',
             'check': 'Running check'}.get(stage, '')
    if not label:
        return ''
    if page := current.get('page'):
        label = f"Evidence page {page['index']}/{page['total']}"
        label += ' saved; verdict pending' if current.get('state') == 'saved' else ' · inspecting'
    if value.get('state') in {'blocked', 'needs_repair'}:
        label = 'Stopped at: ' + label
    return label + (' · ' + target if target else '')


def round_summary(value):
    criteria = (value.get("plan") or {}).get("criteria", [])
    results = {r["criterion_id"]: r for r in value.get("results", [])}
    passed = sum(results.get(c["id"], {}).get("status") == "passed" for c in criteria)
    failed = value["state"] in {"blocked", "needs_repair"} or any(r["status"] in {"failed", "blocked"} for r in results.values())
    title = "Acceptance passed" if value["state"] == "passed" else "Acceptance needs attention" if failed else "Verifying acceptance"
    not_run = bool(criteria) and not any(c["id"] in results for c in criteria)
    if failed and not_run:
        title = "Acceptance blocked before checks"
    progress = f"{len(criteria)} checks pending · No results yet" if not_run else f"{passed}/{len(criteria)} checks passed"
    detail = progress_text(value) or f"Round {value.get('attempts', 0)}"
    return f"{title} · {progress} · {detail}", failed


def round_details(value):
    results = {r["criterion_id"]: r for r in value.get("results", [])}
    lines = [value.get("reason", ""), progress_text(value)]
    if budget := value.get('budget'):
        lines.append(f"Acceptance budget: {budget['model_calls']}/{budget['max_model_calls']} model calls · "
                     f"{budget['elapsed_seconds']:.1f}/{budget['max_seconds']}s · "
                     f"{budget['failed_attempts']}/{budget['max_failed_attempts']} failed candidates")
    for criterion in (value.get("plan") or {}).get("criteria", []):
        result = results.get(criterion["id"], {})
        lines.append(f"[{result.get('status', 'pending')}] {criterion['description']}")
        if result.get("reason"):
            lines.append("  " + result["reason"])
        if result.get("evidence_ids"):
            lines.append("  Evidence: " + ", ".join(result["evidence_ids"]))
        if result.get("artifact", {}).get("sha256"):
            lines.append("  Evidence artifact: " + result["artifact"]["sha256"])
        if result.get("visual_evidence", {}).get("sha256"):
            lines.append("  Browser screenshots: " + result["visual_evidence"]["sha256"] + " (open the evidence in Web)")
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
        if criteria and not any(c["id"] in results for c in criteria):
            progress = f"{len(criteria)} checks pending · No results yet"
        lines = [f"Acceptance · {label} · {progress} · revision {value.get('revision', 0)}"]
        if progress_text(value):
            lines.append(progress_text(value))
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
