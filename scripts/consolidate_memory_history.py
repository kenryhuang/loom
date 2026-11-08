"""Explicit, resumable historical proposal generation; never auto-accepts merges.

Uses the existing Agent budget ledger and memory_jobs receipts. Uncertain delivery
is not retried automatically. Run --plan first, then --run with an existing plan.
"""

import argparse
import asyncio
import json
import sqlite3
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path

from jsonschema import ValidationError, validate

from loom.autonomy.budget import BudgetLedger, BudgetProvider
from loom.evaluation.provider import evaluator_provider
from loom.llm.api import LlmMessage, request_llm_response
from loom.llm.request_options import materialize_request_options
from loom.llm.structured import parse_response
from loom.memory.consolidation import RELATIONS, SYSTEM
from loom.memory.operations import MemoryOperations
from loom.memory.query import available
from loom.memory.repository import PROVENANCE, digest, namespace_id, now
from loom.memory.repository import Repository as BaseRepository
from loom.memory.store import serialized, tokens
from loom.tasks.config import create_provider_from_task_config, load_task_config


@contextmanager
def write_connection(path):
    db = sqlite3.connect(str(path), timeout=60)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class Repository(BaseRepository):
    def connect(self, *, write=False):
        return write_connection(self.path) if write else super().connect()


class HistoryLedger(BudgetLedger):
    def transaction(self):
        return write_connection(self.database)


class ReceiptBudgetProvider(BudgetProvider):
    """Persist a paid response before attempting a contended ledger settlement."""

    def __init__(self, *args, receipt, metadata, **kwargs):
        super().__init__(*args, **kwargs)
        self.receipt, self.metadata = receipt, metadata

    def _settle(self, identifier, response):
        if response is not None:
            temporary = self.receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps(dict(self.metadata, reservation_id=identifier, response=asdict(response)), ensure_ascii=False))
            temporary.replace(self.receipt)
        super()._settle(identifier, response)


PROMPT = (
    SYSTEM
    + """
This is an explicit historical consolidation pass. Input contains a shared node
dictionary and tasks. For EACH task compare its target with ALL listed candidates.
Return JSON only: {"results":[{"task":"...","comparisons":[{"candidate":"...",
"relation":"equivalent|supports|contradicts|different_conditions|supersedes|uncertain",
"reason":"concise Chinese explanation"}]}]}.
Include every task exactly once. Omit unrelated pairs; an empty comparisons list
means all supplied candidates are unrelated. Do not invent candidates.
Semantic fields in record retain all non-provenance content. Evidence contains
immutable observation IDs, root identity and observation time (null means unknown).
Textual paraphrases of a scope can denote the same scope; explain why explicitly.
Different task runs, test counts, working-tree states, dated acceptance results and
delivery artifacts are separate episodes, even when the text is identical. Do not
merge these solely because their numbers or commands match. Unknown observation
times cannot establish supersession. Different dependency evidence IDs alone do
not change the claim, but different dependency requirements do. A weaker claim
and a stronger claim are supports, not equivalent. Be conservative about loss of
preconditions, steps, caveats, temporal applicability, or revalidation conditions.
This output is a proposal, not permission to modify authoritative knowledge.
"""
)
SCHEMA = {
    "type": "object",
    "required": ["results"],
    "additionalProperties": False,
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["task", "comparisons"],
                "additionalProperties": False,
                "properties": {
                    "task": {"type": "string"},
                    "comparisons": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["candidate", "relation", "reason"],
                            "properties": {
                                "candidate": {"type": "string"},
                                "relation": {"enum": RELATIONS},
                                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                            },
                        },
                    },
                },
            },
        }
    },
}


def plan(database, workspace, agent_id):
    repo, ns = Repository(database), namespace_id(workspace)
    with repo.connect() as db:
        agent = json.loads(db.execute("SELECT body FROM agents WHERE id=?", (agent_id,)).fetchone()[0])
        if namespace_id(agent["role"]["workspace"]) != ns:
            raise ValueError("Agent does not own this workspace")
        nodes, tasks = {}, []
        for row in db.execute(
            """SELECT v.id,v.record,v.memory_id,i.latest_revision
            FROM memory_item_versions v JOIN memory_items i ON i.id=v.memory_id
            WHERE i.namespace_id=? AND i.canonical_redirect IS NULL""",
            (ns,),
        ):
            if not available(db, row["id"]):
                continue
            record = json.loads(row["record"])
            observations = [
                dict(r)
                for r in db.execute(
                    """SELECT o.id,o.evidence_group_id,o.observed_at,
                src.session_id,src.evaluation_id,src.record_id FROM memory_supports s
                JOIN memory_observations o ON o.id=s.observation_id JOIN memory_sources src ON src.id=o.source_id
                WHERE s.version_id=? AND src.available=1""",
                    (row["id"],),
                )
            ]
            nodes[row["id"]] = dict(
                version_id=row["id"],
                memory_id=row["memory_id"],
                revision=row["latest_revision"],
                record={k: v for k, v in record.items() if k not in PROVENANCE},
                observations=observations,
            )
        for row in db.execute("SELECT id,input FROM memory_jobs WHERE namespace_id=? AND state='shadow_pending' ORDER BY id", (ns,)):
            value = json.loads(row["input"])
            target = nodes.get(value["version_id"])
            if not target:
                continue
            record = target["record"]
            terms = sorted(tokens(record["title"] + " " + record["summary"]))[:64]
            candidates = [
                r[0]
                for r in db.execute(
                    """SELECT version_id FROM memory_projection
                WHERE namespace_id=? AND kind=? AND (title=? OR summary=?) AND version_id!=? ORDER BY version_id LIMIT 24""",
                    (ns, record["kind"], record["title"], record["summary"], target["version_id"]),
                )
            ]
            if terms:
                match = " OR ".join('"' + t + '"' for t in terms)
                candidates += [
                    r[0]
                    for r in db.execute(
                        """SELECT f.version_id FROM memory_fts f
                    JOIN memory_projection p ON p.version_id=f.version_id
                    WHERE memory_fts MATCH ? AND p.namespace_id=? AND p.kind=? AND f.version_id!=?
                    ORDER BY rank LIMIT 32""",
                        (match, ns, record["kind"], target["version_id"]),
                    )
                ]
            candidates = [v for v in dict.fromkeys(candidates) if v in nodes][:8]
            tasks.append(dict(id=row["id"], target=target["version_id"], candidates=candidates, binding=value.get("binding") or {"agent_id": agent_id}))
    return dict(
        database=str(repo.path),
        namespace=ns,
        agent_id=agent_id,
        created_at=now(),
        nodes=nodes,
        tasks=tasks,
        protocol=digest([PROMPT, SCHEMA]),
        retrieval="exact title/summary then FTS top 8; not exhaustive all-pairs",
    )


def payload_for(tasks, nodes):
    ids = list(dict.fromkeys(v for t in tasks for v in [t["target"], *t["candidates"]]))
    names = {v: str(i) for i, v in enumerate(ids)}
    evidence_names = {}

    def evidence_name(value):
        if value not in evidence_names:
            evidence_names[value] = "e" + str(len(evidence_names))
        return evidence_names[value]

    def compact(node):
        record = dict(node["record"])
        if record.get("title") == record.get("summary"):
            record.pop("title")
        # Evidence hashes are provenance, not dependency requirements. Preserve
        # all other dependencies and every semantic/applicability field.
        if isinstance(record.get("dependencies"), dict):
            record["dependencies"] = {k: v for k, v in record["dependencies"].items() if k != "evidence"}
        return dict(
            record=record,
            evidence=[
                dict(
                    id=evidence_name(o["id"]),
                    root=evidence_name(o["evidence_group_id"]),
                    episode=evidence_name(o["evaluation_id"]),
                    observed_at=o["observed_at"],
                )
                for o in node["observations"]
            ],
        )

    payload = dict(
        nodes={names[v]: compact(nodes[v]) for v in ids},
        tasks=[dict(task=str(i), target=names[t["target"]], candidates=[names[v] for v in t["candidates"]]) for i, t in enumerate(tasks)],
    )
    return payload, {name: vid for vid, name in names.items()}


def batches(tasks, nodes, max_chars=60000, max_tasks=8):
    batch = []
    for task in tasks:
        proposed = [*batch, task]
        if batch and (len(proposed) > max_tasks or len(serialized(payload_for(proposed, nodes)[0])) > max_chars):
            yield batch
            batch = []
        batch.append(task)
    if batch:
        yield batch


def validate_result(result, tasks, inverse):
    validate(result, SCHEMA)
    expected = {str(i) for i in range(len(tasks))}
    if {r["task"] for r in result["results"]} != expected or len(result["results"]) != len(tasks):
        raise ValueError("Missing, duplicated or invented task")
    for row in result["results"]:
        allowed = set(tasks[int(row["task"])]["candidates"])
        seen = set()
        for comparison in row["comparisons"]:
            candidate = inverse.get(comparison["candidate"])
            if candidate not in allowed or candidate in seen:
                raise ValueError("Unknown or duplicate candidate")
            seen.add(candidate)


def repair_receipts(plan_path):
    """Recover only unambiguous wire-format errors from already-paid receipts."""
    history = json.loads(Path(plan_path).read_text())
    repo, nodes = Repository(history["database"]), history["nodes"]
    tasks = {t["id"]: t for t in history["tasks"]}
    repaired = 0
    for path in Path(plan_path).parent.glob("history-*.json"):
        receipt = json.loads(path.read_text())
        with repo.connect() as db:
            failed = {r[0] for r in db.execute("SELECT id FROM memory_jobs WHERE state='history_failed'")}
        if not failed.intersection(receipt["tasks"]):
            continue
        repairs = []

        def unique_pairs(pairs, repairs=repairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    if value[key] != item:
                        raise ValueError("Conflicting duplicate JSON keys")
                    repairs.append("identical duplicate JSON key")
                value[key] = item
            return value

        try:
            raw = receipt["response"]
            if raw["finish_reason"] != "stop":
                continue
            response = json.loads(raw["content"], object_pairs_hook=unique_pairs)
        except (ValueError, KeyError):
            continue
        if not isinstance(response, dict):
            continue
        rows = response.get("results", [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("task"), str) for r in rows):
            continue
        counts = Counter(r.get("task") for r in rows)
        for index, job in enumerate(receipt["tasks"]):
            if job not in failed or counts[str(index)] != 1:
                continue
            row = next(r for r in rows if r.get("task") == str(index))
            if not isinstance(row.get("comparisons"), list) or any(
                not isinstance(comp, dict) or not isinstance(comp.get("candidate"), str) for comp in row["comparisons"]
            ):
                continue
            repairs_for_row = list(repairs)
            comparisons = {}
            for comp in row.get("comparisons", []):
                comp = dict(comp)
                alias = {"support": "supports", "undetermined": "uncertain"}.get(comp.get("relation"))
                if alias:
                    repairs_for_row.append(comp["relation"] + " -> " + alias)
                    comp["relation"] = alias
                prior = comparisons.get(comp.get("candidate"))
                if prior:
                    repairs_for_row.append("duplicate candidate")
                    if prior["relation"] != comp["relation"]:
                        comp.update(relation="uncertain", reason="同一响应对该候选给出不一致判断；保留待复核。")
                comparisons[comp.get("candidate")] = comp
            normalized = dict(task="0", comparisons=list(comparisons.values()))
            try:
                validate_result(dict(results=[normalized]), [tasks[job]], receipt["inverse"])
            except (ValueError, ValidationError):
                continue
            converted = []
            for comp in normalized["comparisons"]:
                vid = receipt["inverse"][comp["candidate"]]
                evidence = [o["id"] for v in [tasks[job]["target"], vid] for o in nodes[v]["observations"]]
                converted.append(dict(candidate_version_id=vid, relation=comp["relation"], reason=comp["reason"], evidence_observation_ids=evidence))
            with repo.connect(write=True) as db:
                source = json.loads(db.execute("SELECT input FROM memory_jobs WHERE id=?", (job,)).fetchone()[0])
                if source.get("history_batch", "").replace(":", "-") + ".json" != path.name:
                    continue
                result = dict(
                    comparisons=converted,
                    model=source["model"],
                    history_batch=source["history_batch"],
                    receipt=str(path),
                    repairs=repairs_for_row,
                    usage_scope="batch",
                    usage=raw["usage"],
                )
                repaired += db.execute(
                    "UPDATE memory_jobs SET state='shadow',result=?,error=NULL WHERE id=? AND state='history_failed'", (serialized(result), job)
                ).rowcount
    print(dict(repaired_from_receipts=repaired))


def apply_reviewed(plan_path, review_path):
    """Apply an explicitly reviewed allowlist, never the model's proposal list."""
    history = json.loads(Path(plan_path).read_text())
    reviewed = json.loads(Path(review_path).read_text())
    repo, ops = Repository(history["database"]), MemoryOperations(history["database"])
    nodes = history["nodes"]
    receipts = []
    for pair in reviewed["approved"].values():
        command = "history-merge:" + pair["id"]
        with repo.connect(write=True) as db:
            old = db.execute("SELECT body FROM memory_events WHERE id=?", (command,)).fetchone()
            if old:
                receipts.append(dict(pair=pair["id"], state="already_applied", command=command))
                continue

            def current(node):
                identifier, visited = node["memory_id"], set()
                while identifier:
                    if identifier in visited:
                        raise ValueError("Canonical redirect cycle")
                    visited.add(identifier)
                    item = db.execute("SELECT * FROM memory_items WHERE id=? AND namespace_id=?", (identifier, history["namespace"])).fetchone()
                    if not item:
                        raise ValueError("Reviewed memory no longer exists in this namespace")
                    if item["canonical_redirect"]:
                        identifier = item["canonical_redirect"]
                        continue
                    version = db.execute(
                        "SELECT id,record FROM memory_item_versions WHERE memory_id=? ORDER BY revision DESC LIMIT 1", (identifier,)
                    ).fetchone()
                    if version["id"] not in nodes or not available(db, version["id"]):
                        raise ValueError("Reviewed memory was changed or became unavailable")
                    record = json.loads(version["record"])
                    if {k: v for k, v in record.items() if k not in PROVENANCE} != nodes[version["id"]]["record"]:
                        raise ValueError("Reviewed content changed")
                    return dict(item), dict(version), record

            try:
                canonical = pair.get("canonical", "a")
                target, target_version, target_record = current(pair[canonical])
                source, source_version, source_record = current(pair["b" if canonical == "a" else "a"])
            except ValueError as exc:
                receipts.append(dict(pair=pair["id"], state="skipped_changed", reason=str(exc)))
                continue
            if source["id"] == target["id"]:
                receipts.append(dict(pair=pair["id"], state="already_same_identity"))
                continue
            payload = dict(
                memory_id=source["id"],
                version_id=source_version["id"],
                expected_revision=source["latest_revision"],
                target_memory_id=target["id"],
                target_revision=target["latest_revision"],
                reason=pair["review_reason"],
                review_artifact=str(review_path),
                reviewed_pair=pair["id"],
            )
            if source_record["scope"] != target_record["scope"]:
                payload["scope_equivalence"] = dict(
                    source=source_record["scope"],
                    target=target_record["scope"],
                    reason=pair["review_reason"] + " 已接受的同义合并链中的范围别名；不修改原始 scope 文本。",
                )
            result = ops.execute(history["namespace"], "MERGE", payload, command_id=command, actor="assistant_history_review", _db=db)
            receipts.append(dict(pair=pair["id"], state="merged", command=command, source_version=source_version["id"], result=result))
            # The job remains a record of all comparisons; accepting one pair
            # does not automatically accept its other proposals.
            if pair.get("job"):
                db.execute("UPDATE memory_jobs SET state='accepted' WHERE id=? AND namespace_id=? AND state='shadow'", (pair["job"], history["namespace"]))
    path = Path(review_path).with_name("applied-merges.json")
    path.write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
    print(dict(merge_receipts=dict(Counter(r["state"] for r in receipts)), artifact=str(path)))


async def run(plan_path, config_path, model, concurrency, limit, batch_size=8):
    history = json.loads(Path(plan_path).read_text())
    repo, nodes = Repository(history["database"]), history["nodes"]
    with repo.connect() as db:
        states = {r["id"]: r["state"] for r in db.execute("SELECT id,state FROM memory_jobs WHERE namespace_id=?", (history["namespace"],))}
    tasks = [t for t in history["tasks"] if states.get(t["id"]) == "shadow_pending"]
    if limit:
        tasks = tasks[:limit]
    base = create_provider_from_task_config(load_task_config(config_path).unwrap(), model_name=model).unwrap()
    options = materialize_request_options(base.request_options)
    options.update(enable_thinking=False, response_format={"type": "json_object"})
    options.pop("reasoning_effort", None)
    provider = evaluator_provider(replace(base, request_options=options, temperature=0), output_tokens=12000, timeout_seconds=180)
    semaphore = asyncio.Semaphore(concurrency)
    counts = Counter()

    async def process(batch):
        with nullcontext():
            batch_id = "history:" + digest([history["protocol"], [t["id"] for t in batch], now()])
            payload, inverse = payload_for(batch, nodes)
            with repo.connect(write=True) as db:
                if any(db.execute("SELECT state FROM memory_jobs WHERE id=?", (t["id"],)).fetchone()[0] != "shadow_pending" for t in batch):
                    return
                for task in batch:
                    old = json.loads(db.execute("SELECT input FROM memory_jobs WHERE id=?", (task["id"],)).fetchone()[0])
                    old.update(
                        model=model,
                        history_temperature=0,
                        history_batch=batch_id,
                        history_protocol=history["protocol"],
                        snapshot=dict(target=nodes[task["target"]], candidates=[nodes[v] for v in task["candidates"]]),
                    )
                    db.execute("UPDATE memory_jobs SET state='history_running',input=?,error=NULL WHERE id=?", (serialized(old), task["id"]))
            # All historical calls belong to this Agent; no attribution to an arbitrary old goal.
            receipt = Path(plan_path).parent / (batch_id.replace(":", "-") + ".json")
            wrapped = ReceiptBudgetProvider(
                provider,
                HistoryLedger(repo.path),
                {"agent_id": history["agent_id"]},
                category="memory_consolidation",
                output_tokens=12000,
                receipt=receipt,
                metadata=dict(payload=payload, inverse=inverse, tasks=[t["id"] for t in batch]),
            )
            try:
                response = await request_llm_response(wrapped, [LlmMessage("system", PROMPT), LlmMessage("user", serialized(payload))], tools=None)
                if not response.ok:
                    raise ValueError(response.error.code + ": " + response.error.message)
                # Persist the response before parsing; failed validation does not lose a paid result.
                result, _ = parse_response(response.value, 150000)
                validate_result(result, batch, inverse)
                with repo.connect(write=True) as db:
                    for row in result["results"]:
                        task = batch[int(row["task"])]
                        comparisons = []
                        for comp in row["comparisons"]:
                            vid = inverse[comp["candidate"]]
                            evidence = [o["id"] for v in [task["target"], vid] for o in nodes[v]["observations"]]
                            comparisons.append(
                                dict(candidate_version_id=vid, relation=comp["relation"], reason=comp["reason"], evidence_observation_ids=evidence)
                            )
                            counts[comp["relation"]] += 1
                        saved = dict(
                            comparisons=comparisons,
                            model=model,
                            model_fingerprint=digest([model, provider.temperature, options, PROMPT, SCHEMA]),
                            history_batch=batch_id,
                            usage_scope="batch",
                            usage=asdict(response.value.usage),
                        )
                        db.execute(
                            "UPDATE memory_jobs SET state='shadow',result=?,error=NULL WHERE id=? AND state='history_running'", (serialized(saved), task["id"])
                        )
                counts["completed"] += len(batch)
            except Exception as exc:
                with repo.connect(write=True) as db:
                    for task in batch:
                        db.execute("UPDATE memory_jobs SET state='history_failed',error=? WHERE id=? AND state='history_running'", (str(exc), task["id"]))
                counts["failed"] += len(batch)
                print(json.dumps(dict(error=str(exc), batch=batch_id), ensure_ascii=False), flush=True)
            print(json.dumps(dict(at=now(), counts=dict(counts)), ensure_ascii=False), flush=True)

    stopped = False

    async def bounded_process(batch):
        nonlocal stopped
        async with semaphore:
            if stopped:
                return
            try:
                # Each batch owns its event loop, so SQLite admission/settlement
                # waits cannot stall delivery of other paid model responses.
                await asyncio.to_thread(lambda: asyncio.run(process(batch)))
            except Exception as exc:
                stopped = True
                print(json.dumps(dict(infrastructure_error=str(exc), resume_required=True)), flush=True)

    await asyncio.gather(*(bounded_process(batch) for batch in batches(tasks, nodes, max_tasks=batch_size)))
    print(json.dumps(dict(done=not stopped, counts=dict(counts)), ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--repair-receipts", action="store_true")
    parser.add_argument("--apply-reviewed", metavar="REVIEW_JSON")
    parser.add_argument("--database", default="runs/service/service.sqlite")
    parser.add_argument("--workspace")
    parser.add_argument("--agent")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--model", default="dashscope/deepseek-v4.1-flash-1017")
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=6)
    parser.add_argument("--batch-size", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.plan:
        if Path(args.artifact).exists():
            raise ValueError("Refusing to overwrite historical plan")
        value = plan(args.database, args.workspace, args.agent)
        Path(args.artifact).parent.mkdir(parents=True, exist_ok=True)
        Path(args.artifact).write_text(json.dumps(value, ensure_ascii=False))
        print(dict(nodes=len(value["nodes"]), tasks=len(value["tasks"]), batches=len(list(batches(value["tasks"], value["nodes"])))))
    if args.run:
        asyncio.run(run(args.artifact, args.config, args.model, args.concurrency, args.limit, args.batch_size))
    if args.repair_receipts:
        repair_receipts(args.artifact)
    if args.apply_reviewed:
        apply_reviewed(args.artifact, args.apply_reviewed)


if __name__ == "__main__":
    main()
