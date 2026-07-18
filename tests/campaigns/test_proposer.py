from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.proposer import MeteredProposalResult, ProposalRequest, ProposalUsage, SandboxProposerAdapter, SubprocessProposerAdapter
from loom.campaigns.sandbox import LengthBoundedChannel
from loom.campaigns.workspace import CandidateWorkspace
from loom.core import ok


def _request(max_candidates: int = 2) -> ProposalRequest:
    return ProposalRequest(
        "cmp_test",
        iteration=1,
        max_candidates=max_candidates,
        remaining_budget={"proposer_tokens": 1000, "cost": "1.00"},
        editable_surfaces=("context_policy",),
        forbidden_surfaces=("evaluator",),
    )


def _draft() -> dict:
    return {
        "kind": "declarative_patch",
        "parent_ids": [],
        "inspiration_ids": [],
        "hypothesis": {
            "problem": "Context is too large.",
            "mechanism": "Reduce the bounded context budget.",
            "expected_improvements": [{"metric_id": "total_tokens", "expected_delta": -100}],
            "expected_regressions": [{"metric_id": "task_success", "expected_delta": -0.01}],
            "preserved_behaviors": ["workspace confinement"],
        },
        "evidence_refs": ["ev-1"],
        "changed_surfaces": ["context_policy"],
        "artifact_path": "patch.json",
        "patch_path": "patch.json",
        "capability_manifest": {
            "imports": [],
            "dependencies": [],
            "filesystem_read_roots": [],
            "filesystem_write_roots": [],
            "callable_tools": [],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "resource_limits": {},
        },
    }


def test_subprocess_proposer_defaults_to_fail_closed_without_isolation(tmp_path: Path):
    workspace = CandidateWorkspace.allocate(tmp_path / "workspaces", "cand_workspace").unwrap()
    adapter = SubprocessProposerAdapter((sys.executable, "missing.py"), ArtifactStore(tmp_path / "artifacts"))

    result = asyncio.run(adapter.propose(_request(), None, workspace))

    assert not result.ok
    assert result.error.code == "SANDBOX_UNAVAILABLE"


def test_trusted_local_proposer_has_empty_environment_and_records_complete_session(tmp_path: Path):
    script = tmp_path / "proposer.py"
    drafts = [_draft()]
    script.write_text(
        "import json, os\n"
        "json.dump(dict(os.environ), open('environment.json', 'w'))\n"
        f"json.dump({drafts!r}, open('candidate-drafts.json', 'w'))\n"
        "print('proposal complete')\n",
        encoding="utf-8",
    )
    workspace = CandidateWorkspace.allocate(tmp_path / "workspaces", "cand_workspace").unwrap()
    store = ArtifactStore(tmp_path / "artifacts")
    adapter = SubprocessProposerAdapter((sys.executable, str(script)), store, trusted_local_debug=True, timeout_seconds=5)

    result = asyncio.run(adapter.propose(_request(), None, workspace))

    assert result.ok and len(result.value.drafts) == 1
    assert result.value.usage.proposer_tokens == 0
    assert result.value.usage.wall_time_seconds >= 0
    environment = json.loads((workspace.root / "environment.json").read_text())
    assert set(environment).issubset({"LC_CTYPE", "__CF_USER_TEXT_ENCODING"})
    assert all("KEY" not in key and "TOKEN" not in key and "SECRET" not in key for key in environment)
    assert adapter.last_session_ref is not None
    session = json.loads(store.read_bytes(adapter.last_session_ref).unwrap())
    assert session["returncode"] == 0
    assert "proposal complete" in session["stdout"]
    assert session["request"]["campaign_id"] == "cmp_test"


def test_proposer_rejects_malformed_excess_and_duplicate_drafts(tmp_path: Path):
    async def run_case(name: str, payload, max_candidates=2):
        script = tmp_path / f"{name}.py"
        script.write_text(f"import json\njson.dump({payload!r}, open('candidate-drafts.json', 'w'))\n", encoding="utf-8")
        workspace = CandidateWorkspace.allocate(tmp_path / "workspaces", name).unwrap()
        adapter = SubprocessProposerAdapter(
            (sys.executable, str(script)),
            ArtifactStore(tmp_path / f"artifacts-{name}"),
            trusted_local_debug=True,
            timeout_seconds=5,
        )
        return await adapter.propose(_request(max_candidates), None, workspace)

    malformed = asyncio.run(run_case("malformed", {"not": "a list"}))
    excess = asyncio.run(run_case("excess", [_draft(), {**_draft(), "artifact_path": "other.json"}], max_candidates=1))
    duplicate = asyncio.run(run_case("duplicate", [_draft(), _draft()]))

    for result in (malformed, excess, duplicate):
        assert not result.ok
        assert result.error.code == "PROPOSAL_FAILED"


def test_proposer_timeout_is_propagated_and_session_is_recorded(tmp_path: Path):
    script = tmp_path / "timeout.py"
    script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    workspace = CandidateWorkspace.allocate(tmp_path / "workspaces", "timeout").unwrap()
    adapter = SubprocessProposerAdapter(
        (sys.executable, str(script)),
        ArtifactStore(tmp_path / "artifacts"),
        trusted_local_debug=True,
        timeout_seconds=0.05,
    )

    result = asyncio.run(adapter.propose(_request(), None, workspace))

    assert not result.ok
    assert result.error.code == "TIMEOUT"
    assert adapter.last_session_ref is not None


def test_isolated_proposer_uses_framed_backend_and_persists_session(tmp_path: Path):
    class Backend:
        def __init__(self):
            self.request = None

        async def run(self, spec, mounts, frame):
            del spec, mounts
            channel = LengthBoundedChannel(100_000)
            self.request = json.loads(channel.decode(frame).unwrap())
            output = channel.encode(
                json.dumps(
                    {
                        "schema_version": "loom.proposal-batch.v1",
                        "drafts": [_draft()],
                    },
                    separators=(",", ":"),
                ).encode()
            )
            return ok(MeteredProposalResult(output.unwrap(), ProposalUsage(42, "0.04", 3)))

    backend = Backend()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    adapter = SandboxProposerAdapter(
        backend,
        spec=object(),
        mounts=object(),
        artifact_store=artifacts,
        max_frame_bytes=100_000,
    )

    result = asyncio.run(adapter.propose(_request(), history=object(), workspace=object()))

    assert result.ok and len(result.value.drafts) == 1
    assert result.value.usage.proposer_tokens == 42
    assert backend.request["campaign_id"] == "cmp_test"
    assert "validation" not in json.dumps(backend.request)
    assert "holdout" not in json.dumps(backend.request)
    assert adapter.last_session_ref is not None
