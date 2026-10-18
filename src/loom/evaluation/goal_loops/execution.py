"""Versioned work queues, checkpoint upgrades and deterministic reconciliation candidates."""

from copy import deepcopy
from difflib import SequenceMatcher

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.state import overlaps

LEGACY_VERSION = ("goal-loops.1", "goal-loops-rubric.1")
UPGRADABLE_VERSIONS = {LEGACY_VERSION, ("goal-loops.2", "goal-loops-rubric.2")}
PROTOCOL_VERSION = 2


def upgrade_checkpoint(checkpoint, expected):
    cp = deepcopy(checkpoint)
    old = cp.get("identity", {})
    if not old or old == expected:
        return cp
    comparable = {k: v for k, v in old.items() if k not in {"analyzer", "prompt"}}
    expected_source = {k: v for k, v in expected.items() if k not in {"analyzer", "prompt"}}
    if (old.get("analyzer"), old.get("prompt")) not in UPGRADABLE_VERSIONS or comparable != expected_source:
        raise ValueError("Goal-loop checkpoint identity changed; start a new evaluation")
    cp.setdefault("protocol_migrations", []).append(
        dict(
            from_identity=old,
            to_identity=expected,
            stages=deepcopy(cp.get("stages", {})),
            unfinished=deepcopy(cp.get("stage_work", {})),
            reconcile_groups=deepcopy(cp.get("reconcile_groups", [])),
            graph_revision=cp.get("graph_revision", 0),
            diagnostics=deepcopy(cp.get("entities", {}).get("diagnostics", [])),
        )
    )
    cp["identity"] = expected
    cp["execution_protocol"] = PROTOCOL_VERSION
    cp["stage_work"] = {}
    # Retain validated entities, revisions, source coverage, completed structural work and usage.
    # Recompute downstream candidates under the narrower protocol, without resending old work.
    retained = ("structure:", "reconcile:") if old.get("analyzer") == "goal-loops.2" else ("structure:",)
    cp["stages"] = {k: v for k, v in cp.get("stages", {}).items() if k.startswith(retained)}
    for key in ("reconcile_groups", "reconcile_candidates", "reconciliation_complete", "diagnosis_groups", "complete"):
        cp.pop(key, None)
    cp["diagnosed_goals"] = []
    cp["diagnosis_windows"] = {}
    if cp.get("entities"):
        cp["entities"]["diagnostics"] = []  # Regenerated against utility, retained above for audit.
    cp.setdefault("limitations", []).append("Action utility enabled; saved structure and usage retained; downstream stages rescheduled")
    return cp


def reconciliation_candidates(state):
    candidates = []
    for kind, parent, text in [("goals", "parent_goal_id", "objective"), ("attempts", "goal_id", "approach")]:
        rows = [r for r in state[kind] if r.get("basis") == "inferred" and not r.get("superseded_by")]
        for i, a in enumerate(rows):
            for b in rows[i + 1 :]:
                if a.get(parent) != b.get(parent):
                    continue
                left, right = " ".join(a.get(text, "").lower().split()), " ".join(b.get(text, "").lower().split())
                shared = any(overlaps(x, y) for x in a.get("evidence_refs", []) for y in b.get("evidence_refs", []))
                if not left or not right:
                    continue
                match = left == right and (kind == "goals" or shared)
                match |= shared and SequenceMatcher(None, left, right).ratio() >= 0.7
                if match:
                    candidates.append(
                        dict(
                            id="candidate:" + identity(a["id"], b["id"]),
                            source_id=a["id"],
                            target_id=b["id"],
                            reason="Same-parent entities have similar intent and overlapping evidence" if shared else "Repeated goal objective",
                        )
                    )
    return candidates


def split_unit(unit, registry, derived):
    """Persist a strictly smaller request. A one-pair reconciliation is already atomic."""
    depth = unit.get("depth", 0)
    if depth >= 6:
        return []
    if unit["family"] == "utility" and len(unit.get("action_ids", [])) > 1:
        field = "action_ids"
    elif unit["family"] == "reconcile":
        field = "candidate_ids"
        if len(unit.get(field, [])) < 2:
            return []
    elif len(unit["packet_ids"]) > 1:
        field = "packet_ids"
    elif len(unit.get("object_ids", [])) > 1:
        field = "object_ids"
    else:
        if not unit["packet_ids"]:
            return []
        packet = registry[unit["packet_ids"][0]]
        ref = packet.get("evidence_ref")
        if not ref or len(packet["content"]) <= 512:
            return []
        middle = len(packet["content"]) // 2
        parts = []
        for start, end in [(0, middle), (middle, len(packet["content"]))]:
            precise = {**ref, "start": (ref.get("start") or 0) + start, "end": (ref.get("start") or 0) + end}
            identifier = "p:" + identity(precise)
            child = {
                **packet,
                "id": identifier,
                "evidence_ref": precise,
                "content": packet["content"][start:end],
                "full_field": False,
                "original_id": packet.get("original_id", packet["id"]),
            }
            registry[identifier] = derived[identifier] = child
            parts.append([identifier])
        field = "packet_ids"
    if "parts" not in locals():
        values = unit[field]
        middle = len(values) // 2
        parts = [values[:middle], values[middle:]]
    return [
        {**deepcopy(unit), field: part, "id": unit["id"] + suffix, "depth": depth + 1, "read_round": 0}
        for suffix, part in zip((".a", ".b"), parts, strict=True)
    ]


def record_review(cp, delivered, references, registry):
    for pid, packet in delivered.items():
        original = packet.get("original_id", pid)
        ref = packet.get("evidence_ref")
        if not ref:
            cp["packet_status"].setdefault(original, "indexed_only")
            continue
        cp.setdefault("page_reads", {}).setdefault(original, []).append([ref.get("start") or 0, ref["end"]])
        if pid in references:
            cp.setdefault("page_classified", {})[original] = True
        source = registry[original]["evidence_ref"]
        end = source.get("start") or 0
        for a, b in sorted(cp["page_reads"][original]):
            if a > end:
                break
            end = max(end, b)
        if end >= source["end"]:
            state = "classified" if cp.get("page_classified", {}).get(original) else "reviewed_unknown"
            if cp["packet_status"].get(original) != "classified":
                cp["packet_status"][original] = state
