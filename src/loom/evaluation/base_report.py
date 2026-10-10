"""Markdown view of the same statistics consumed by the web client."""


def render_base_report(base):
    s = base["statistics"]
    b, tools, models, timing, quality = (s[k] for k in ("basic", "tools", "models", "timing", "quality"))

    def seconds(ms):
        return "not recorded" if ms is None else f"{ms / 1000:.2f} s"

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["## Base evaluation", "", "### 1. Basic information", "", f"Execution: {base['execution_status']}",
             f"Events: {b['events']['raw']} raw / {b['events']['analyzed']} analyzed",
             f"Runs: {b['runs']} · Start attempts: {b['run_attempts']} · Logical steps: {b['steps']} · Step starts: {b['step_attempts']}",
             f"Model calls: {models['calls']} · Tool calls: {tools['calls']}"]
    acceptance = (b.get("acceptance") or {}).get("state")
    if acceptance:
        lines += [f"Acceptance: {acceptance['state']} · Rounds: {acceptance.get('attempts', 0)}"]
        descriptions = {c["id"]: c["description"] for c in (acceptance.get("plan") or {}).get("criteria", [])}
        lines += [f"- {r['status']}: {descriptions.get(r['criterion_id'], r['criterion_id'])} — {r.get('reason', '')}"
                  for r in acceptance.get("results", [])]
    else:
        lines += ["Acceptance: no checks recorded for the latest execution."]
    lines += ["", "### 2. Tool calls", "", quality["success_rate_basis"], "",
              "| Tool | Calls | Success | Failed | Cancelled | Incomplete | Unknown | Cumulative time | P95 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in tools["by_tool"]:
        lines.append("| " + " | ".join(cell(v) for v in (r["name"], r["calls"], r["success"], r["failed"], r["cancelled"],
                     r["incomplete"], r["unknown"], seconds(r["duration"]["total_ms"]), seconds(r["duration"]["p95_ms"]))) + " |")
    lines += ["", "### 3. Runtime cost", "", f"Elapsed across runs: {seconds(timing['wall_ms'])}"]
    lines += [f"- {k}: {seconds(v)}" for k, v in timing["states_ms"].items()]
    lines += ["", "Activity within recorded running intervals:"]
    lines += [f"- {k}: {seconds(v)}" for k, v in timing["running_activity_ms"].items()]
    lines += ["", *timing["limitations"], "", "### 4. Model calls and token cost", ""]
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        r = models[field]
        lines.append(f"- {field}: {r['known']} known · {r['measured_calls']}/{models['calls']} calls measured")
    for r in models["by_role"]:
        lines.append(f"- {r['name']}: {r['calls']} calls · {r['total_tokens']['known']} known tokens · {seconds(r['duration']['total_ms'])}")
    lines += ["", "### 5. Outputs and data completeness", ""]
    lines += [f"- Workspace: {p}" for p in s["outputs"]["workspace_files"]]
    lines += [f"- Artifact: {r['kind']} · {r.get('relative_path', r.get('local_path', ''))}" for r in s["outputs"]["artifacts"]]
    lines += [f"Missing total usage: {quality['missing_usage_calls']} calls. Missing duration: {quality['missing_call_durations']} calls.",
              quality["token_basis"], quality["retry_basis"], "Unavailable measurements: " + "; ".join(quality["unavailable"]), ""]
    return lines
