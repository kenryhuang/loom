"""Staged candidate manifest and declarative patch validation."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from loom.campaigns.contracts import ArtifactRef, CandidateDraft, CandidateKind, CandidatePolicy, CapabilityManifest
from loom.campaigns.materialization import CompiledDeclarativeCandidate, DeclarativePatchCompiler
from loom.campaigns.sandbox import LengthBoundedChannel, RootlessContainerSandbox
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.core import Result, err, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    stages: tuple[str, ...]
    compiled: CompiledDeclarativeCandidate | None = None
    executable: ExecutableValidation | None = None


@dataclass(frozen=True, slots=True)
class ExecutableValidation:
    imports: tuple[str, ...]
    ast_nodes: int
    source_files: int
    source_bytes: int
    source_digest: str


@dataclass(frozen=True, slots=True)
class SandboxExecutableSmokeValidator:
    backend: RootlessContainerSandbox
    spec: Any
    mounts: Any
    max_frame_bytes: int = 1024 * 1024

    async def validate(
        self,
        sources: Mapping[str, str],
        required_function: str,
        capability: CapabilityManifest,
    ) -> Result:
        channel = LengthBoundedChannel(self.max_frame_bytes)
        request = channel.encode(
            canonical_json_bytes(
                {
                    "schema_version": "loom.executable-smoke-request.v1",
                    "sources": sources,
                    "required_function": required_function,
                    "input_schema": capability.input_schema,
                    "output_schema": capability.output_schema,
                }
            )
        )
        if not request.ok:
            return request
        result = await self.backend.run(self.spec, self.mounts, request.value)
        if not result.ok:
            return result
        if not isinstance(result.value, bytes):
            return _source_error("INTERFACE_INVALID", "Executable smoke sandbox returned a non-byte frame")
        decoded = channel.decode(result.value)
        if not decoded.ok:
            return decoded
        try:
            payload = json.loads(decoded.value)
            if (
                payload.get("schema_version") != "loom.executable-smoke.v1"
                or payload.get("required_function") != required_function
                or payload.get("passed") is not True
            ):
                raise ValueError("sandbox interface smoke failed")
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _source_error("INTERFACE_INVALID", "Executable smoke sandbox rejected the component", cause=str(exc))
        return ok(None)


def publish_validation_evidence(
    store,
    *,
    campaign_id: str,
    candidate_id: str,
    candidate_ref: ArtifactRef,
    result: ValidationResult,
    policy_digest: str,
    actor,
) -> Result:
    authorized = store.identity_provider.authorize(
        actor,
        "campaign_controller",
        forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
    )
    if not authorized.ok:
        return authorized
    verified = store.artifacts.read_bytes(candidate_ref)
    if not verified.ok:
        return verified
    claims = {
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_ref": candidate_ref,
        "policy_digest": policy_digest,
        "valid": result.valid,
        "stages": result.stages,
        "compiled_result_digest": None if result.compiled is None else result.compiled.result_digest,
        "executable_source_digest": None if result.executable is None else result.executable.source_digest,
    }
    payload = {
        "schema_version": "loom.candidate-validation.v1",
        "claims": claims,
        "attestation": {"actor": asdict(actor), "claims_digest": canonical_digest(claims)},
    }
    return store.artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="candidate_validation",
        schema_version="loom.candidate-validation.v1",
        suffix=".json",
    )


def load_validation_evidence(
    store,
    ref: ArtifactRef,
    *,
    campaign_id: str,
    candidate_id: str,
    policy_digest: str,
) -> Result:
    read = store.artifacts.read_bytes(ref, expected_schema="loom.candidate-validation.v1")
    if not read.ok:
        return read
    try:
        envelope = json.loads(read.value)
        if envelope["schema_version"] != "loom.candidate-validation.v1":
            raise ValueError("validation schema does not match")
        payload = envelope["claims"]
        attestation = envelope["attestation"]
        from loom.core import ActorAssertion

        actor = ActorAssertion(**attestation["actor"])
        authorized = store.identity_provider.authorize(
            actor,
            "campaign_controller",
            forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
        )
        if not authorized.ok:
            return authorized
        if attestation["claims_digest"] != canonical_digest(payload):
            raise ValueError("validation attestation digest does not match")
        candidate_ref = ArtifactRef(**payload["candidate_ref"])
        if (
            payload["campaign_id"] != campaign_id
            or payload["candidate_id"] != candidate_id
            or payload["policy_digest"] != policy_digest
            or not isinstance(payload["valid"], bool)
            or not isinstance(payload["stages"], list)
        ):
            raise ValueError("validation identity, policy, or schema does not match")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "CANDIDATE_MANIFEST_INVALID",
                "Candidate validation evidence is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )
    candidate = store.artifacts.read_bytes(candidate_ref)
    return candidate if not candidate.ok else ok((payload["valid"], candidate_ref, tuple(payload["stages"])))


class DefaultCandidateValidator:
    def __init__(
        self,
        workspace,
        compiler: DeclarativePatchCompiler,
        executable_smoke: SandboxExecutableSmokeValidator | None = None,
    ):
        self.workspace = workspace
        self.compiler = compiler
        self.executable_smoke = executable_smoke

    async def validate(self, candidate: CandidateDraft, policy: CandidatePolicy) -> Result:
        manifest = _validate_manifest(candidate, policy)
        if not manifest.ok:
            return manifest
        if candidate.kind is CandidateKind.EXECUTABLE_COMPONENT:
            path = self.workspace.resolve(candidate.artifact_path)
            if not path.ok:
                return path
            try:
                payload = json.loads(path.value.read_text(encoding="utf-8"))
                sources = payload["sources"]
                required_function = str(payload["required_function"])
                if not isinstance(sources, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in sources.items()):
                    raise TypeError("executable sources must map paths to source text")
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                return err(
                    make_loom_error(
                        "CANDIDATE_COMPONENT_INVALID",
                        "Executable candidate artifact is malformed",
                        retryable=False,
                        cause={"name": type(exc).__name__, "message": str(exc)},
                    )
                )
            executable = validate_executable_source(sources, candidate.capability_manifest, policy, required_function=required_function)
            if not executable.ok:
                return executable
            if self.executable_smoke is None:
                return err(
                    make_loom_error(
                        "SANDBOX_UNAVAILABLE",
                        "Executable interface validation requires a rootless sandbox",
                        retryable=False,
                    )
                )
            smoke = await self.executable_smoke.validate(sources, required_function, candidate.capability_manifest)
            if not smoke.ok:
                return smoke
            return ok(ValidationResult(True, ("manifest", "source", "capability", "sandbox_interface"), executable=executable.value))
        if candidate.patch_path is None:
            return _manifest_error("Declarative candidate patch path is required", stage="manifest")
        path = self.workspace.resolve(candidate.patch_path)
        if not path.ok:
            return path
        try:
            payload = json.loads(path.value.read_text(encoding="utf-8"))
            base = payload["base"]
            operations = tuple(payload["operations"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            return err(
                make_loom_error(
                    "CANDIDATE_PATCH_INVALID",
                    "Candidate patch artifact is malformed",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                    metadata={"stage": "declarative_patch"},
                )
            )
        compiled = self.compiler.compile(base, operations, policy, evidence_trace_ids=candidate.evidence_refs)
        if not compiled.ok:
            return compiled
        return ok(ValidationResult(True, ("manifest", "declarative_patch", "inverse_smoke"), compiled.value))


def _validate_manifest(candidate: CandidateDraft, policy: CandidatePolicy) -> Result:
    if candidate.kind not in policy.kinds:
        return _manifest_error("Candidate kind is not allowed", stage="manifest")
    if not candidate.evidence_refs:
        return _manifest_error("Candidate requires evidence references", stage="manifest")
    if not candidate.changed_surfaces or not set(candidate.changed_surfaces).issubset(policy.editable_surfaces):
        return _manifest_error("Candidate changed surfaces are not editable", stage="manifest")
    if set(candidate.changed_surfaces) & set(policy.forbidden_surfaces):
        return _manifest_error("Candidate changed surfaces include forbidden surfaces", stage="manifest")
    hypothesis = candidate.hypothesis
    if not hypothesis.problem.strip() or not hypothesis.mechanism.strip():
        return _manifest_error("Candidate hypothesis must state a problem and mechanism", stage="manifest")
    if not hypothesis.expected_improvements or not hypothesis.expected_regressions or not hypothesis.preserved_behaviors:
        return _manifest_error("Candidate hypothesis must include predictions and preserved behaviors", stage="manifest")
    return ok(None)


def validate_executable_source(
    sources: Mapping[str, str],
    capability: CapabilityManifest,
    policy: CandidatePolicy,
    *,
    required_function: str,
) -> Result:
    if not policy.executable_enabled or CandidateKind.EXECUTABLE_COMPONENT not in policy.kinds:
        return _source_error("EXECUTABLE_FORBIDDEN", "Executable candidates are disabled")
    if not sources or len(sources) > policy.max_source_files:
        return _source_error("SOURCE_LIMIT_EXCEEDED", "Executable candidate source file count exceeds policy")
    source_bytes = sum(len(value.encode("utf-8")) for value in sources.values())
    if source_bytes > policy.max_source_bytes:
        return _source_error("SOURCE_LIMIT_EXCEEDED", "Executable candidate source bytes exceed policy")
    imports: set[str] = set()
    ast_nodes = 0
    required_found = False
    digester = hashlib.sha256()
    for path, source in sorted(sources.items()):
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or pure.suffix != ".py":
            return _source_error("PATH_FORBIDDEN", "Executable source path is invalid", path=path)
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            return _source_error("SOURCE_SYNTAX_INVALID", "Executable candidate does not parse", path=path, line=exc.lineno)
        nodes = tuple(ast.walk(tree))
        ast_nodes += len(nodes)
        if ast_nodes > policy.max_ast_nodes:
            return _source_error("SOURCE_LIMIT_EXCEEDED", "Executable candidate AST exceeds policy")
        for node in nodes:
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    return _source_error("DYNAMIC_IMPORT_FORBIDDEN", "Relative imports are forbidden", path=path)
                if node.module:
                    imports.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.FunctionDef) and node.name == required_function:
                required_found = True
            elif isinstance(node, ast.Call):
                rejected = _validate_call(node)
                if rejected is not None:
                    return rejected
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                alias_error = _forbidden_callable_reference(node.id)
                if alias_error is not None:
                    return alias_error
            elif isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in {"load_module", "exec_module", "find_spec"}):
                return _source_error("REFLECTION_FORBIDDEN", "Loader, environment, and dunder reflection are forbidden", path=path)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.casefold()
                if any(marker in lowered for marker in ("terminalbench", "benchmark answer", "reference solution", "expected answer")):
                    return _source_error("BENCHMARK_REFERENCE_FORBIDDEN", "Benchmark-specific strings are forbidden", path=path)
        digester.update(path.encode())
        digester.update(b"\0")
        digester.update(source.encode())
    forbidden_imports = imports - set(policy.allowed_imports)
    if forbidden_imports:
        return _source_error("IMPORT_FORBIDDEN", "Executable candidate imports are not allowlisted", imports=sorted(forbidden_imports))
    if imports != set(capability.imports):
        return _source_error(
            "CAPABILITY_MISMATCH",
            "Capability manifest imports do not match static analysis",
            declared=sorted(capability.imports),
            observed=sorted(imports),
        )
    if capability.dependencies:
        return _source_error("CAPABILITY_MISMATCH", "Third-party dependencies are not allowed by the initial executable profile")
    if capability.network_required or capability.subprocess_required:
        return _source_error("CAPABILITY_MISMATCH", "Network and subprocess capabilities are unavailable")
    if capability.callable_tools:
        return _source_error(
            "CAPABILITY_MISMATCH",
            "Executable candidates cannot call supervisor tools or inference brokers",
            callable_tools=capability.callable_tools,
        )
    if set(capability.filesystem_write_roots) - {"/scratch"}:
        return _source_error("CAPABILITY_MISMATCH", "Executable write roots exceed /scratch")
    if set(capability.filesystem_read_roots) - {"/candidate", "/workspace", "/runtime"}:
        return _source_error("CAPABILITY_MISMATCH", "Executable read roots exceed the sandbox view")
    if not required_found:
        return _source_error("INTERFACE_INVALID", "Executable candidate does not implement the required function")
    return ok(ExecutableValidation(tuple(sorted(imports)), ast_nodes, len(sources), source_bytes, digester.hexdigest()))


def _validate_call(node: ast.Call) -> Result | None:
    name = _call_name(node.func)
    if name in {"eval", "exec", "compile"}:
        return _source_error("DYNAMIC_EXECUTION_FORBIDDEN", "Dynamic code execution is forbidden")
    if name == "__import__":
        return _source_error("DYNAMIC_IMPORT_FORBIDDEN", "Dynamic import is forbidden")
    if name in {"getattr", "setattr", "delattr", "globals", "locals", "vars"}:
        return _source_error("REFLECTION_FORBIDDEN", "Runtime reflection is forbidden")
    if name == "open" and node.args:
        value = _constant_string(node.args[0])
        if value is None:
            return _source_error("PATH_FORBIDDEN", "Dynamic file paths are forbidden")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            return _source_error("PATH_FORBIDDEN", "Absolute and parent-traversal paths are forbidden")
        if any(part in {".env", "credentials", "secrets"} for part in path.parts):
            return _source_error("SECRET_ACCESS_FORBIDDEN", "Secret-bearing files are forbidden")
    return None


def _forbidden_callable_reference(name: str) -> Result | None:
    if name in {"eval", "exec", "compile"}:
        return _source_error("DYNAMIC_EXECUTION_FORBIDDEN", "Dynamic code execution references are forbidden")
    if name == "__import__":
        return _source_error("DYNAMIC_IMPORT_FORBIDDEN", "Dynamic import references are forbidden")
    if name in {"getattr", "setattr", "delattr", "globals", "locals", "vars", "__builtins__"}:
        return _source_error("REFLECTION_FORBIDDEN", "Runtime reflection references are forbidden")
    if name == "open":
        return _source_error("PATH_FORBIDDEN", "Raw filesystem callable references are forbidden")
    return None


def _call_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _constant_string(value: ast.expr) -> str | None:
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _manifest_error(message: str, **metadata) -> Result:
    return err(make_loom_error("CANDIDATE_MANIFEST_INVALID", message, retryable=False, metadata=metadata))


def _source_error(code: str, message: str, **metadata: Any) -> Result:
    return err(make_loom_error(code, message, retryable=False, metadata=metadata))


__all__ = [
    "DefaultCandidateValidator",
    "ExecutableValidation",
    "SandboxExecutableSmokeValidator",
    "ValidationResult",
    "load_validation_evidence",
    "publish_validation_evidence",
    "validate_executable_source",
]
