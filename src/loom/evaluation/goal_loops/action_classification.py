"""Evidence-backed presentation classification; never execute shell or inspect hidden reasoning."""

import json
import re
import shlex
from collections.abc import Mapping

from loom.core import thaw_json

READ_COMMANDS = {"find", "ls", "cat", "head", "tail", "rg", "grep", "pwd", "wc"}
ROLES = {"evidence": "research", "root_cause": "research", "artifact": "implementation", "verification": "verification", "delivery": "final_answer"}
PHASES = {"approach": "planning", "investigation": "research", "implementation": "implementation", "verification": "verification", "conclusion": "final_answer"}


def inspection_command(command):
    """Recognize a small shell grammar, using the full input rather than its UI excerpt."""
    if not isinstance(command, str) or len(command) > 131072:
        return False
    command = re.sub(r"(?<!\S)2>\s*/dev/null(?=\s|$)", "", command)
    if any(x in command for x in ("$(", "`", ">", "<")):
        return False
    command = command.strip()
    if command.startswith("for "):
        loop = re.fullmatch(r"for\s+[a-zA-Z_]\w*\s+in\s+([^;\n]+);\s*do\s+(.+);\s*done\s*;?", command, re.S)
        if not loop or re.search(r"[$&|(){}]", loop[1]):
            return False
        command = loop[2]
    try:
        lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return False
    chunks, chunk = [], []
    for word in words:
        if word in {";", "&&", "||", "|"}:
            if not chunk:
                return False
            chunks.append(chunk)
            chunk = []
        else:
            chunk.append(word)
    if chunk:
        chunks.append(chunk)
    if not chunks or not any(c[0] in READ_COMMANDS for c in chunks):
        return False
    return all(c[0] in READ_COMMANDS | {"echo", "printf"} and not any(
        re.match(r"^-(?:exec|execdir|ok|okdir|delete|fprint0?|fprintf|fls)$|^--pre(?:=|$)", w) or w in {"&", "(", ")"} for w in c[1:]
    ) for c in chunks)


def _owned_refs(source, row, refs):
    owned = []
    lines = {ref["line_number"] for ref in row["evidence_refs"]}
    for ref in refs or []:
        try:
            event = source.event(ref)
            source.resolve(ref)
            if event.line_number in lines:
                owned.append(ref)
        except (ValueError, KeyError, TypeError):
            continue
    return owned


def _declared_purpose(source, row, caller):
    if not caller or not caller.get("statement_refs") or not row.get("input_refs"):
        return None
    ref = {**caller["statement_refs"][0], "start": None, "end": None}
    try:
        content = source.resolve(ref)
        if not isinstance(content, str):
            return None
        content = content.strip()
        if content.startswith("```json") and content.endswith("```"):
            content = content[7:-3].strip()
        data = json.loads(content)
        action = data.get("action") if isinstance(data, dict) else None
        inp = thaw_json(source.resolve(row["input_refs"][0]))
        if isinstance(inp, dict) and {"id", "at", "source", "value"} <= inp.keys():
            inp = inp["value"]
        if not isinstance(action, dict) or action.get("kind") != "tool" or action.get("target") != row["tool"] or action.get("input") != inp:
            return None
        description = action.get("description")
        if not isinstance(description, str):
            return None
        # This is the solver's declared intention, not proof of successful verification.
        if re.match(r"^(?:verify\b|cross[- ]check\b|run (?:a |the )?(?:numeric(?:al)? audit|verification|test)\b)", description, re.I):
            return dict(kind="verification", title="Run verification", basis="inferred from declared action purpose",
                        inferred=True, rationale=description, evidence_refs=[ref, *row["input_refs"]])
    except (ValueError, TypeError):
        pass
    return None


def classify_actions(source, rows, bundle):
    from loom.evaluation.goal_loops.action_flow import LABELS, classify

    by_id = {r["id"]: r for r in rows}
    for row in rows:
        # Reconstruct the command only for classification; keep UI targets bounded.
        probe = dict(row)
        if row.get("input_refs"):
            inp = thaw_json(source.resolve(row["input_refs"][0]))
            if isinstance(inp, Mapping) and {"id", "at", "source", "value"} <= inp.keys():
                inp = inp["value"]
            if isinstance(inp, Mapping) and isinstance(inp.get("command"), str):
                probe["target"] = inp["command"]
        kind, title, basis = classify(probe)
        classification = dict(kind=kind, title=title, basis=basis, inferred=False, evidence_refs=row["evidence_refs"])
        if kind == "other":
            utility = row.get("utility") or {}
            refs = _owned_refs(source, row, utility.get("evidence_refs"))
            inferred = ROLES.get(utility.get("role"))
            if utility.get("status") == "reviewed" and utility.get("confidence") in {"high", "medium"} and inferred and refs:
                classification = dict(kind=inferred, title=LABELS[inferred], basis="inferred from saved action assessment",
                                      inferred=True, rationale=utility.get("reason", ""), evidence_refs=refs)
            else:
                candidates = []
                for phase in bundle.get("phases", []):
                    refs = _owned_refs(source, row, phase.get("evidence_refs"))
                    inferred = PHASES.get(phase.get("kind"))
                    if (not phase.get("superseded_by") and row["id"] in phase.get("call_ids", []) and inferred and refs
                            and phase.get("confidence") in {"high", "medium"}):
                        candidates.append(dict(kind=inferred, title=LABELS[inferred], basis="inferred from saved phase assessment",
                                               inferred=True, rationale=phase.get("question", ""), evidence_refs=refs))
                if candidates and len({p["kind"] for p in candidates}) == 1:
                    classification = candidates[0]
                elif candidates:
                    classification["basis"] = "conflicting saved phase assessments"
                elif row["kind"] == "tool":
                    classification = _declared_purpose(source, row, by_id.get(row.get("caller_id"))) or classification
        row["classification"] = classification
