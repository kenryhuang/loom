"""Example public API for Loom."""

from loom.examples.factories import (
    make_chain_pipeline_example,
    make_fork_reviewers_example,
    make_initial_counter_context,
    make_initial_llm_context,
    make_level1_evolution_example,
    make_llm_loop_definition,
    make_minimal_counter_loop,
    make_nested_tool_example,
    make_structure_evolution_example,
    read_openai_api_key_from_env,
    run_context_boundary_example,
    run_llm_loop,
    run_trace_query_example,
)
from loom.examples.real_project_smoke import (
    ProjectInfo,
    RealProjectSmokeConfig,
    inspect_project,
    synthesize_report,
)

__all__ = [
    "ProjectInfo",
    "RealProjectSmokeConfig",
    "inspect_project",
    "make_chain_pipeline_example",
    "make_fork_reviewers_example",
    "make_initial_counter_context",
    "make_initial_llm_context",
    "make_level1_evolution_example",
    "make_llm_loop_definition",
    "make_minimal_counter_loop",
    "make_nested_tool_example",
    "make_structure_evolution_example",
    "read_openai_api_key_from_env",
    "run_context_boundary_example",
    "run_llm_loop",
    "run_trace_query_example",
    "synthesize_report",
]
