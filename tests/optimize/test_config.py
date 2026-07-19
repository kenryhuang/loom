from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from loom.optimize.config import load_optimize_config
from loom.optimize.contracts import OptimizationLifecycle, OptimizationStage, SplitRatios


def _config_text(*, reasoning_effort: str = "high", meta_extra: str = "", split: str = "") -> str:
    split_block = (
        split
        or """    split:
      discovery: 0.60
      validation: 0.20
      holdout: 0.20
"""
    )
    return f"""default_model: kimi
models:
  main:
    provider: openai
    model: proposer-model
    base_url: https://example.test/v1
    api_key: test-key
    max_completion_tokens: 4096
  kimi:
    provider: openai
    model: solver-model
    base_url: https://example.test/v1
    api_key: test-key
    max_completion_tokens: 8192
    request_options:
      reasoning_effort: {reasoning_effort}
      stream_options:
        include_usage: true
  kimi_judge:
    provider: openai
    model: judge-model
    base_url: https://example.test/v1
    api_key: test-key
    max_completion_tokens: 4096
    request_options:
      reasoning_effort: high
meta_harness:
  proposer_model: main
  solver_model: kimi
  judge_model: kimi_judge
  tasks:
{split_block}{meta_extra}"""


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_optimize_config_resolves_meta_harness_defaults(tmp_path: Path):
    loaded = load_optimize_config(_write_config(tmp_path, _config_text())).unwrap()

    assert loaded.meta.proposer_model == "main"
    assert loaded.meta.solver_model == "kimi"
    assert loaded.meta.judge_model == "kimi_judge"
    assert loaded.meta.tasks.split == SplitRatios(Decimal("0.60"), Decimal("0.20"), Decimal("0.20"))
    assert loaded.meta.tasks.repetitions == 3
    assert loaded.meta.search.iterations == 4
    assert loaded.meta.governance.auto_promote_max_risk == "low"
    assert loaded.task_config.models["kimi"].max_completion_tokens == 8192


def test_load_optimize_config_rejects_unknown_meta_harness_field(tmp_path: Path):
    result = load_optimize_config(_write_config(tmp_path, _config_text(meta_extra="  surprise: true\n")))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "meta_harness.surprise"


def test_load_optimize_config_rejects_invalid_reasoning_effort(tmp_path: Path):
    result = load_optimize_config(_write_config(tmp_path, _config_text(reasoning_effort="max")))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "models.kimi.request_options.reasoning_effort"


def test_load_optimize_config_rejects_split_that_does_not_sum_to_one(tmp_path: Path):
    split = """    split:
      discovery: 0.50
      validation: 0.20
      holdout: 0.20
"""
    result = load_optimize_config(_write_config(tmp_path, _config_text(split=split)))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "meta_harness.tasks.split"


def test_load_optimize_config_rejects_missing_model_reference(tmp_path: Path):
    result = load_optimize_config(_write_config(tmp_path, _config_text().replace("judge_model: kimi_judge", "judge_model: absent")))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "meta_harness.judge_model"


def test_load_optimize_config_rejects_fewer_than_five_valid_pairs(tmp_path: Path):
    result = load_optimize_config(_write_config(tmp_path, _config_text(meta_extra="    minimum_pairs: 4\n")))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "meta_harness.tasks.minimum_pairs"


def test_load_optimize_config_rejects_primary_metric_not_emitted_by_trials(tmp_path: Path):
    result = load_optimize_config(_write_config(tmp_path, _config_text(meta_extra="  objectives:\n    primary: invented_metric\n")))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "meta_harness.objectives.primary"


def test_load_optimize_config_rejects_executable_without_approval(tmp_path: Path):
    executable = """  search:
    candidate_kinds:
      - executable_component
  governance:
    require_approval_for:
      - medium
      - high
"""
    result = load_optimize_config(_write_config(tmp_path, _config_text(meta_extra=executable)))

    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"
    assert result.error.metadata["field"] == "meta_harness.governance.require_approval_for"


def test_optimize_lifecycle_contract_values_are_stable():
    assert OptimizationStage.HOLDOUT_COMPLETE.value == "holdout_complete"
    assert OptimizationLifecycle.AWAITING_APPROVAL.value == "awaiting_approval"
