"""Fail-closed executable candidate isolation contracts."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from loom.core import Result, err, make_loom_error, ok

_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class IsolationCapabilities:
    rootless: bool
    read_only_root: bool
    mount_namespace: bool
    user_namespace: bool
    no_new_privs: bool
    seccomp: bool
    network_namespace: bool
    cgroups: bool
    disk_quota: bool

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in self.__dataclass_fields__ if getattr(self, name) is not True)


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    candidate_root: str
    workspace_root: str
    runtime_root: str
    scratch_root: str
    memory_mb: int
    cpu_seconds: int
    process_limit: int
    disk_mb: int
    output_bytes: int
    network: bool = False
    purpose: Literal["candidate", "proposer"] = "candidate"
    workspace_writable: bool = False

    def __post_init__(self) -> None:
        roots = (self.candidate_root, self.workspace_root, self.runtime_root, self.scratch_root)
        if roots != ("/candidate", "/workspace", "/runtime", "/scratch"):
            raise ValueError("Executable sandbox uses fixed canonical mount roots")
        if self.network:
            raise ValueError("Promotion evidence sandboxes cannot enable network access")
        if self.workspace_writable != (self.purpose == "proposer"):
            raise ValueError("Only proposer sandboxes receive a writable workspace")
        limits = (self.memory_mb, self.cpu_seconds, self.process_limit, self.disk_mb, self.output_bytes)
        if any(isinstance(value, bool) or value <= 0 for value in limits):
            raise ValueError("Sandbox resource limits must be positive")


@dataclass(frozen=True, slots=True)
class SandboxMounts:
    """Controller-owned host paths exposed read-only to one trial.

    Scratch is intentionally absent: the production backend creates a fresh,
    size-bounded tmpfs for every container invocation.
    """

    candidate_host: Path
    workspace_host: Path
    runtime_host: Path

    def resolved(self) -> tuple[Path, Path, Path]:
        values = tuple(Path(value).resolve(strict=True) for value in self._values())
        if any(not value.is_dir() for value in values):
            raise ValueError("Sandbox mounts must be existing directories")
        workspace = values[1]
        forbidden_names = frozenset({".env", ".git", ".ssh", ".aws", "credentials", "credentials.json", "id_rsa", "id_ed25519"})
        for root in values:
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Sandbox mount trees cannot contain symlinks")
                if root == workspace and (path.name.casefold() in forbidden_names or ".config/gcloud" in path.as_posix().casefold()):
                    raise ValueError("Sandbox workspace must be a sanitized snapshot without credentials or VCS metadata")
        return values  # type: ignore[return-value]

    def _values(self) -> tuple[Path, Path, Path]:
        return self.candidate_host, self.workspace_host, self.runtime_host


class RootlessContainerRuntime(Protocol):
    async def probe(self) -> Result: ...

    async def run(self, spec: SandboxSpec, mounts: SandboxMounts, input_frame: bytes) -> Result: ...


class UnsupportedIsolationBackend:
    def __init__(self, reason: str):
        self.reason = reason

    async def run(self, spec: SandboxSpec, mounts: SandboxMounts, input_frame: bytes) -> Result:
        del spec, mounts, input_frame
        return _sandbox_unavailable(self.reason)


class RootlessContainerSandbox:
    """Admission wrapper that trusts only a runtime's live capability probe."""

    def __init__(self, runtime: RootlessContainerRuntime):
        self.runtime = runtime

    async def run(self, spec: SandboxSpec, mounts: SandboxMounts, input_frame: bytes) -> Result:
        capabilities = await self.runtime.probe()
        if not capabilities.ok:
            return capabilities
        if not isinstance(capabilities.value, IsolationCapabilities):
            return _sandbox_unavailable("Isolation runtime returned an invalid capability report")
        missing = capabilities.value.missing()
        if missing:
            return _sandbox_unavailable(
                "Required rootless container profile is unavailable",
                missing_capabilities=missing,
            )
        try:
            mounts.resolved()
        except (OSError, ValueError) as exc:
            return _sandbox_unavailable(
                "Sandbox mount set is invalid",
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        return await self.runtime.run(spec, mounts, input_frame)


class PodmanRootlessRuntime:
    """Concrete rootless Podman adapter with a controller-generated command.

    No caller-provided command, environment, network option, mount target, or
    entrypoint crosses this boundary. A failed probe or run rejects executable
    evaluation; there is deliberately no subprocess fallback.
    """

    def __init__(self, *, image: str, seccomp_profile: str | Path, executable: str = "podman"):
        profile = Path(seccomp_profile).resolve(strict=False)
        if not _PINNED_IMAGE.fullmatch(image):
            raise ValueError("Executable sandbox image must be pinned by sha256 digest")
        if not profile.is_file():
            raise ValueError("Executable sandbox requires an explicit seccomp profile")
        if Path(executable).name != "podman":
            raise ValueError("Only the Podman rootless runtime is supported")
        self.image = image
        self.seccomp_profile = profile
        self.executable = executable

    async def probe(self) -> Result:
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "info",
                "--format=json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_empty_runtime_environment(),
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode != 0 or len(stdout) > 1024 * 1024:
                return _sandbox_unavailable("Podman capability probe failed")
            info = json.loads(stdout)
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            return _sandbox_unavailable(
                "Podman capability probe failed",
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        host = info.get("host", {}) if isinstance(info, dict) else {}
        security = host.get("security", {}) if isinstance(host, dict) else {}
        rootless = bool(security.get("rootless"))
        seccomp_enabled = bool(security.get("seccompEnabled", security.get("seccomp_enabled", False)))
        cgroup_version = str(host.get("cgroupVersion", host.get("cgroup_version", ""))).lower()
        # The remaining controls are explicit, mandatory Podman run flags. If
        # the installed runtime cannot enforce one, the actual run fails closed.
        return ok(
            IsolationCapabilities(
                rootless=rootless,
                read_only_root=True,
                mount_namespace=True,
                user_namespace=True,
                no_new_privs=True,
                seccomp=seccomp_enabled,
                network_namespace=True,
                cgroups=cgroup_version in {"v2", "2"},
                disk_quota=True,
            )
        )

    def build_command(self, spec: SandboxSpec, mounts: SandboxMounts) -> tuple[str, ...]:
        candidate, workspace, runtime = mounts.resolved()
        read_only_mount = "--mount=type=bind,src={source},destination={target},ro=true,relabel=private"
        workspace_mount = "--mount=type=bind,src={source},destination={target},relabel=private"
        if not spec.workspace_writable:
            workspace_mount += ",ro=true"
        entrypoint = {
            "candidate": "/runtime/loom-candidate-runner",
            "proposer": "/runtime/loom-proposer-runner",
        }[spec.purpose]
        return (
            self.executable,
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--userns=keep-id",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--security-opt=seccomp={self.seccomp_profile}",
            f"--pids-limit={spec.process_limit}",
            f"--memory={spec.memory_mb}m",
            "--memory-swap=0",
            f"--ulimit=cpu={spec.cpu_seconds}:{spec.cpu_seconds}",
            "--ulimit=nofile=64:64",
            f"--storage-opt=size={spec.disk_mb}m",
            read_only_mount.format(source=candidate, target=spec.candidate_root),
            workspace_mount.format(source=workspace, target=spec.workspace_root),
            read_only_mount.format(source=runtime, target=spec.runtime_root),
            f"--tmpfs={spec.scratch_root}:rw,noexec,nosuid,nodev,size={spec.disk_mb}m",
            "--workdir=/workspace",
            self.image,
            entrypoint,
        )

    async def run(self, spec: SandboxSpec, mounts: SandboxMounts, input_frame: bytes) -> Result:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.build_command(spec, mounts),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_empty_runtime_environment(),
            )
        except (OSError, ValueError) as exc:
            return _sandbox_unavailable(
                "Rootless container failed to start",
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        try:
            process.stdin.write(input_frame)
            await process.stdin.drain()
            process.stdin.close()
            stdout_task = asyncio.create_task(_bounded_drain(process.stdout, spec.output_bytes))
            stderr_task = asyncio.create_task(_bounded_drain(process.stderr, 8192))
            stdout_result, _, returncode = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, process.wait()),
                timeout=spec.cpu_seconds + 5,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except TimeoutError:
            process.kill()
            await process.wait()
            return err(make_loom_error("TIMEOUT", "Executable candidate exceeded its runtime limit", retryable=False))
        if stdout_result[1]:
            return _frame_error("Sandbox output exceeds its byte limit")
        if returncode != 0:
            return err(
                make_loom_error(
                    "CANDIDATE_COMPONENT_FAILED",
                    "Executable candidate exited unsuccessfully",
                    retryable=False,
                    metadata={"returncode": returncode},
                )
            )
        return ok(stdout_result[0])


async def _bounded_drain(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    kept = bytearray()
    overflow = False
    while chunk := await stream.read(64 * 1024):
        remaining = limit + 1 - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        overflow = overflow or len(kept) > limit or len(chunk) > remaining
    return bytes(kept[:limit]), overflow


def _empty_runtime_environment() -> dict[str, str]:
    # This environment belongs to the trusted Podman client, not the container.
    # Candidate processes inherit none of it because the run command has no
    # `--env-host` or host-derived `--env` flags.
    environment = {"HOME": str(Path.home()), "PATH": os.defpath, "LANG": "C.UTF-8"}
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        environment["XDG_RUNTIME_DIR"] = runtime_dir
    return environment


class LengthBoundedChannel:
    def __init__(self, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def encode(self, payload: bytes) -> Result:
        if len(payload) > self.max_bytes:
            return _frame_error("Sandbox output exceeds its byte limit")
        return ok(str(len(payload)).encode("ascii") + b"\n" + payload)

    def decode(self, frame: bytes) -> Result:
        header, separator, payload = frame.partition(b"\n")
        if not separator or not header.isdigit():
            return _frame_error("Sandbox frame header is malformed")
        length = int(header)
        if length > self.max_bytes or length != len(payload):
            return _frame_error("Sandbox frame length is invalid")
        return ok(payload)


def _sandbox_unavailable(message: str, *, cause=None, **metadata) -> Result:
    return err(
        make_loom_error(
            "SANDBOX_UNAVAILABLE",
            message,
            retryable=False,
            cause=cause,
            metadata=metadata,
        )
    )


def _frame_error(message: str) -> Result:
    return err(make_loom_error("SANDBOX_OUTPUT_INVALID", message, retryable=False))


__all__ = [
    "IsolationCapabilities",
    "LengthBoundedChannel",
    "PodmanRootlessRuntime",
    "RootlessContainerRuntime",
    "RootlessContainerSandbox",
    "SandboxMounts",
    "SandboxSpec",
    "UnsupportedIsolationBackend",
]
