from __future__ import annotations

import pytest

from loom.campaigns.contracts import CandidateKind, CandidatePolicy, CapabilityManifest
from loom.campaigns.validation import validate_executable_source


def _policy() -> CandidatePolicy:
    return CandidatePolicy(
        kinds=(CandidateKind.EXECUTABLE_COMPONENT,),
        editable_surfaces=("context_policy",),
        forbidden_surfaces=("evaluator", "permissions", "provider", "secrets"),
        allowed_patch_operations=(),
        allowed_imports=("json", "math"),
        max_source_files=2,
        max_source_bytes=4096,
        max_ast_nodes=200,
        executable_enabled=True,
    )


def _manifest(**overrides) -> CapabilityManifest:
    values = {
        "imports": ("json",),
        "dependencies": (),
        "filesystem_read_roots": ("/candidate", "/workspace"),
        "filesystem_write_roots": ("/scratch",),
        "callable_tools": (),
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "resource_limits": {"memory_mb": 128},
        "subprocess_required": False,
        "network_required": False,
        **overrides,
    }
    return CapabilityManifest(**values)


def test_executable_source_accepts_narrow_protocol_without_privileged_capabilities():
    source = "import json\n\ndef build_context(payload):\n    return {'context': json.dumps(payload)}\n"

    result = validate_executable_source({"candidate.py": source}, _manifest(), _policy(), required_function="build_context")

    assert result.ok
    assert result.value.imports == ("json",)
    assert result.value.ast_nodes > 0


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("import os\ndef build_context(x): return os.environ\n", "IMPORT_FORBIDDEN"),
        ("import socket\ndef build_context(x): return socket.socket()\n", "IMPORT_FORBIDDEN"),
        ("import subprocess\ndef build_context(x): return subprocess.run(['id'])\n", "IMPORT_FORBIDDEN"),
        ("def build_context(x): return open('../secret').read()\n", "PATH_FORBIDDEN"),
        ("def build_context(x): return open('/etc/passwd').read()\n", "PATH_FORBIDDEN"),
        ("def build_context(x): return open('.env').read()\n", "SECRET_ACCESS_FORBIDDEN"),
        ("def build_context(x): return eval(x['code'])\n", "DYNAMIC_EXECUTION_FORBIDDEN"),
        ("def build_context(x): return __import__(x['module'])\n", "DYNAMIC_IMPORT_FORBIDDEN"),
        ("import json\ndef build_context(x): return json.__loader__.load_module('os').environ\n", "REFLECTION_FORBIDDEN"),
        ("def build_context(x): return 'terminalbench expected answer'\n", "BENCHMARK_REFERENCE_FORBIDDEN"),
    ],
)
def test_executable_source_rejects_escape_network_secret_process_and_benchmark_patterns(source: str, code: str):
    result = validate_executable_source({"candidate.py": source}, _manifest(), _policy(), required_function="build_context")

    assert not result.ok
    assert result.error.code == code


def test_executable_source_rejects_false_capability_declaration_and_missing_interface():
    undeclared = validate_executable_source(
        {"candidate.py": "import math\ndef build_context(x): return math.ceil(1.2)\n"},
        _manifest(imports=("json",)),
        _policy(),
        required_function="build_context",
    )
    missing = validate_executable_source(
        {"candidate.py": "import json\ndef other(x): return x\n"},
        _manifest(),
        _policy(),
        required_function="build_context",
    )

    assert not undeclared.ok and undeclared.error.code == "CAPABILITY_MISMATCH"
    assert not missing.ok and missing.error.code == "INTERFACE_INVALID"


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("def build_context(x):\n    f = open\n    return f('/etc/passwd')\n", "PATH_FORBIDDEN"),
        ("def build_context(x):\n    f = __import__\n    return f('subprocess')\n", "DYNAMIC_IMPORT_FORBIDDEN"),
        ("def build_context(x):\n    g = getattr\n    return g(x, '__class__')\n", "REFLECTION_FORBIDDEN"),
    ],
)
def test_executable_source_rejects_forbidden_builtin_aliases(source: str, code: str):
    result = validate_executable_source({"candidate.py": source}, _manifest(imports=()), _policy(), required_function="build_context")

    assert not result.ok and result.error.code == code
