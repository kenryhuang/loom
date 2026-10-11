import asyncio
import copy

from loom.evaluation.behavior_budget import _pages, scan_pages, select_affordable, synthesis_inputs
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import judge_behavior
from tests.evaluation.test_behavior import BehaviorJudge, calls, source


def long_index():
    return {"segments": [{"id": f"s{i}", "episode_id": "ep", "goal_revision_id": "g", "round_ids": [f"r{i}"],
                          "boundary_key": ["ep", "g", "loop", "trace", i],
                          "evidence_refs": [{"source_sha256": "a" * 64, "line_number": i * 30 + j,
                                             "event_hash": None, "field_path": None, "start": None, "end": None} for j in range(20)],
                          "navigation": [{"round_id": f"r{i}", "response_excerpt": "排查根因并进行验证" * 40,
                                          "tools": [{"tool_id": "shell_execute", "status": "complete", "output_excerpt": "已找到证据" * 40}]}]}
                         for i in range(23)]}


def test_scan_compaction_preserves_all_segments_and_saved_legacy_prefix():
    facts = long_index()
    legacy_index = [{k: s[k] for k in ("id", "episode_id", "goal_revision_id", "evidence_refs", "navigation")} for s in facts["segments"]]
    legacy = _pages(legacy_index, 16000, legacy=True)
    assert len(legacy) > 3
    saved = {f"scan:{i}": {"result": {"candidate_segment_ids": [legacy[i][0]["id"]]}} for i in range(3)}
    cp = {"stages": copy.deepcopy(saved), "scan_pages": len(legacy)}
    pages = scan_pages(facts, cp, 48000)
    assert cp["stages"] == saved
    assert cp["scan_page_ids"][:3] == [[s["id"] for s in p] for p in legacy[:3]]
    assert [s["id"] for page in pages for s in page] == [s["id"] for s in facts["segments"]]
    assert len(pages) < len(legacy)
    assert all(not s["evidence_refs"] for page in pages for s in page)
    assert scan_pages(facts, cp, 48000) == pages
    assert len(scan_pages(facts, {}, 48000)) < len(legacy)


def test_scan_can_continue_beyond_old_stage_quota_without_raising_total_limits(tmp_path):
    store = source(tmp_path, calls(2))
    facts, checkpoint = build_behavior_facts(store), {}
    asyncio.run(judge_behavior(store, facts, BehaviorJudge(), checkpoint=checkpoint, max_calls=24, max_tokens=120000))
    # Historical calls can consume a former stage quota even when total capacity remains.
    checkpoint["stages"] = {}
    checkpoint.pop("selected")
    checkpoint["usage"].update(calls=3, total_tokens=24454)
    checkpoint["stage_usage"] = {"scan": {"calls": 3, "total_tokens": 24454}}
    judge = BehaviorJudge()
    result = asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint, max_calls=24, max_tokens=120000)).unwrap()
    assert result["coverage"]["status"] == "complete"
    assert judge.calls == 3
    assert result["usage"]["calls"] == 6
    assert result["usage"]["total_tokens"] == 24499


def test_focus_sample_is_budget_planned_and_resume_keeps_its_original_scope():
    cp = {"usage": {"calls": 5, "total_tokens": 40000}, "stage_usage": {"scan": {"prompt_tokens": 20000, "request_chars": 60000}}}
    ids = [f"s{i}" for i in range(8)]
    args = dict(max_segments=8, max_calls=24, max_tokens=120000, max_read_rounds=1)
    selected = select_affordable(ids, dict.fromkeys(ids, 40000), cp, **args)
    assert 1 <= len(selected) < 8
    assert cp["selection_budget"]["budget_limited"]
    cp["selected"] = selected
    cp["usage"]["total_tokens"] = 110000
    assert select_affordable(ids, dict.fromkeys(ids, 40000), cp, **args) == selected


def test_synthesis_reuses_only_bounded_cited_evidence_not_all_focus_context():
    ref = {"source_sha256": "a" * 64, "line_number": 1}
    local = {"diagnoses": [{"id": "d", "dimension": "plan_quality", "observation": "Evidence", "mechanism": "Test",
                             "supporting_refs": [ref], "counterevidence_refs": [], "validation": "Long experimental design" * 1000}],
             "preserved_behaviors": [], "coverage": {}, "evidence_packets": [{"content": "unused context" * 10000}]}
    payload, packets = synthesis_inputs(local, 48000)
    assert "validation" not in payload["diagnoses"][0]
    assert len(packets) == 1 and packets[0]["returned_ref"] == ref
    assert packets[0]["returned_chars"] <= 600
