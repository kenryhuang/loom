"""Readable event details, prioritizing task content over transport metadata."""

import json
import re

INTERNAL = re.compile(
    r"^(?:id|.*_id|.*_ids|.*_digest|.*_sha256|sha256|fingerprint|at|timestamp|type|schema(?:_version)?|revision|.*_revision|metadata|usage|binding|artifact|messages|tools|view|failed)$"
)
PRIORITY = (
    "description", "objective", "summary", "reason", "validation_error", "error", "message", "content", "text", "proposal", "criteria",
    "requirements", "unresolved_requirements", "limitations", "plan", "workflow", "result", "results", "scope", "path", "status", "state",
)


def meaningful_fields(value):
    sections = []
    shortened = False

    def walk(value, path="", depth=0):
        nonlocal shortened
        if value is None or value == "":
            return
        if len(sections) >= 40 or depth > 7:
            shortened = True
            return
        if isinstance(value, dict):
            keys = sorted((k for k in value if not INTERNAL.fullmatch(k)), key=lambda k: PRIORITY.index(k) if k in PRIORITY else len(PRIORITY))
            for key in keys:
                walk(value[key], " · ".join(filter(None, (path, key.replace("_", " ")))), depth + 1)
        elif isinstance(value, (list, tuple)):
            for index, entry in enumerate(value[:20], 1):
                walk(entry, f"{path} {index}".strip(), depth + 1)
            shortened |= len(value) > 20
        else:
            text = str(value)
            sections.append((path, text[:6000]))
            shortened |= len(text) > 6000

    walk(value)
    if shortened:
        sections.append(("More details", "Preview shortened. Full content remains in the recorded event."))
    return sections


def event_content(kind, data):
    if kind.startswith("acceptance.plan."):
        acceptance = data.get("acceptance") or {}
        plan = (data.get("proposal") if kind == "acceptance.plan.proposed" else acceptance.get("plan")) or {}
        titles = {"proposed": "Proposed acceptance plan", "accepted": "Acceptance plan ready", "revised": "Acceptance plan revised",
                  "rejected": "Acceptance proposal rejected", "repairing": "Correcting acceptance plan"}
        sections = []
        if data.get("validation_error"):
            sections.append(("Problem", data["validation_error"]))
        criteria = plan.get("criteria", []) if isinstance(plan, dict) else []
        for index, criterion in enumerate(criteria if isinstance(criteria, list) else [], 1):
            if not isinstance(criterion, dict):
                continue
            lines = [str(criterion.get("description") or "No criterion description recorded")]
            scope = criterion.get("scope")
            if isinstance(scope, list) and scope:
                lines.append("Scope: " + ", ".join(str(p) for p in scope))
            methods = {"semantic": "Review the result and supporting evidence", "command": "Run a verification command",
                       "artifact": "Check the deliverable", "source": "Check source evidence", "external": "External confirmation"}
            if criterion.get("verifier"):
                lines.append("Verification: " + methods.get(criterion["verifier"], criterion["verifier"]))
            check = criterion.get("check") if isinstance(criterion.get("check"), dict) else {}
            args = check.get("input") or {}
            if isinstance(args, dict):
                command = args.get("command") or (" ".join(map(str, args["argv"])) if isinstance(args.get("argv"), list) else "")
                if command:
                    lines.append("Command: " + str(command))
            if check.get("path"):
                lines.append("Deliverable: " + check["path"])
            sections.append((f"Criterion {index}", "\n".join(lines)))
        if isinstance(plan, dict):
            sections.extend(meaningful_fields({"unresolved_requirements": plan.get("unresolved_requirements"),
                "reason": plan.get("revision_reason") or acceptance.get("reason")}))
        return titles.get(kind.rsplit(".", 1)[-1], "Acceptance plan"), sections
    if kind == "workspace.probed":
        profile = data.get("profile") or {}
        return "Workspace inspected", meaningful_fields({k: profile.get(k) for k in ("workspace", "limitations", "configuration", "files")})
    if kind.startswith("workflow.node."):
        workflow = data.get("workflow") or {}
        active = next((n for n in workflow.get("workflow", {}).get("nodes", []) if n.get("id") == workflow.get("active")), None)
        return kind.replace(".", " "), meaningful_fields(active or {"plan": data.get("plan")})
    if kind == "artifact.created":
        artifact = data.get("artifact") or {}
        if artifact.get("kind") in {"verification_evidence", "verification"}:
            content = data.get("artifact_content")
            title = "Verification evidence saved" if artifact["kind"] == "verification_evidence" else "Verification check saved"
            return title, verification_artifact_fields(content) if content is not None else [("", "Verification content loads when expanded.")]
        return "Artifact saved", meaningful_fields({"path": artifact.get("relative_path") or artifact.get("local_path"),
            "kind": artifact.get("kind"), "byte_size": artifact.get("byte_size")})
    return None, meaningful_fields(data)


def verification_artifact_fields(value):
    if not isinstance(value, dict):
        return meaningful_fields(value)
    sections = [("", "Recorded verification check." if value.get("criterion") else
        "Evidence supplied to the acceptance review. This bundle is not the final acceptance verdict.")]
    sections.extend(meaningful_fields({"goal": value.get("goal")}))
    plan = value.get("plan") or {"criteria": [value["criterion"]] if value.get("criterion") else []}
    sections.extend(event_content("acceptance.plan.accepted", {"acceptance": {"plan": plan}})[1])
    sections.extend(meaningful_fields({"result": value.get("result"), "detail": value.get("detail")}))
    for index, evidence in enumerate(value.get("evidence", []), 1):
        label = "Candidate answer under review" if evidence.get("id") == "candidate" else f"Evidence {index} · {evidence.get('tool', 'recorded evidence')}"
        if evidence.get("content"):
            sections.append((label, str(evidence["content"])))
        sections.extend(meaningful_fields({label: {"input": evidence.get("input")}}))
        output = evidence.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                if output.lstrip().startswith(("{", "[")):
                    output = "The recorded structured output is incomplete; only a fragment was retained."
        if isinstance(output, dict) and ("stdout" in output or "stderr" in output):
            output = {k: output[k] for k in ("stdout", "stderr", "exit_code") if k in output}
        sections.extend(meaningful_fields({label: {"output": output, "result": evidence.get("result"), "detail": evidence.get("detail")}}))
        if evidence.get("truncated"):
            sections.append((label, "Recorded output was truncated; omitted content was not included in this evidence."))
    return sections


def readable_event_details(kind, data):
    _, sections = event_content(kind, data)
    return "\n\n".join(f"{label}\n{value}" if label else value for label, value in sections) or "No additional details recorded."
