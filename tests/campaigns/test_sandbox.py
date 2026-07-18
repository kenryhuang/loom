from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from loom.campaigns.sandbox import (
    IsolationCapabilities,
    LengthBoundedChannel,
    PodmanRootlessRuntime,
    RootlessContainerSandbox,
    SandboxMounts,
    SandboxSpec,
    UnsupportedIsolationBackend,
)
from loom.core import ok


def _spec() -> SandboxSpec:
    return SandboxSpec(
        candidate_root="/candidate",
        workspace_root="/workspace",
        runtime_root="/runtime",
        scratch_root="/scratch",
        memory_mb=256,
        cpu_seconds=30,
        process_limit=16,
        disk_mb=64,
        output_bytes=4096,
        network=False,
    )


def test_unsupported_backend_rejects_instead_of_falling_back_to_subprocess():
    result = asyncio.run(UnsupportedIsolationBackend("container runtime unavailable").run(_spec(), _mounts(None), b"{}"))

    assert not result.ok
    assert result.error.code == "SANDBOX_UNAVAILABLE"


def test_rootless_backend_requires_complete_enforcement_profile():
    incomplete = IsolationCapabilities(
        rootless=True,
        read_only_root=True,
        mount_namespace=True,
        user_namespace=True,
        no_new_privs=True,
        seccomp=False,
        network_namespace=True,
        cgroups=True,
        disk_quota=True,
    )

    class Runtime:
        async def probe(self):
            return ok(incomplete)

        async def run(self, spec, mounts, input_frame):  # pragma: no cover - must not run
            raise AssertionError("incomplete runtime must not execute")

    backend = RootlessContainerSandbox(Runtime())

    result = asyncio.run(backend.run(_spec(), _mounts(tmp_path=None), b"{}"))

    assert not result.ok
    assert result.error.code == "SANDBOX_UNAVAILABLE"
    assert "seccomp" in result.error.metadata["missing_capabilities"]


def test_sandbox_spec_rejects_network_and_noncanonical_mounts_for_promotion_evidence():
    with pytest.raises(ValueError):
        SandboxSpec("candidate", "/workspace", "/runtime", "/scratch", 1, 1, 1, 1, 1, False)
    with pytest.raises(ValueError):
        SandboxSpec("/candidate", "/workspace", "/runtime", "/scratch", 1, 1, 1, 1, 1, True)


def test_length_bounded_channel_round_trips_and_rejects_oversized_or_malformed_frames():
    channel = LengthBoundedChannel(max_bytes=16)
    frame = channel.encode(b'{"ok":true}').unwrap()

    assert channel.decode(frame).unwrap() == b'{"ok":true}'
    assert not channel.encode(b"x" * 17).ok
    assert not channel.decode(b"999\n{} ").ok


def _mounts(tmp_path: Path | None) -> SandboxMounts:
    root = tmp_path or Path("/")
    return SandboxMounts(
        candidate_host=root / "candidate",
        workspace_host=root / "workspace",
        runtime_host=root / "runtime",
    )


def test_podman_runtime_builds_fixed_fail_closed_command(tmp_path):
    for name in ("candidate", "workspace", "runtime", "scratch"):
        (tmp_path / name).mkdir()
    seccomp = tmp_path / "seccomp.json"
    seccomp.write_text(json.dumps({"defaultAction": "SCMP_ACT_ERRNO"}), encoding="utf-8")
    runtime = PodmanRootlessRuntime(
        image="loom-candidate@sha256:" + "a" * 64,
        seccomp_profile=seccomp,
        executable="/usr/bin/podman",
    )

    command = runtime.build_command(_spec(), _mounts(tmp_path))
    joined = " ".join(command)

    assert command[:3] == ("/usr/bin/podman", "run", "--rm")
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "no-new-privileges" in joined
    assert f"seccomp={seccomp.resolve()}" in joined
    assert "--pids-limit=16" in command
    assert "--memory=256m" in command
    assert "--storage-opt=size=64m" in command
    assert "loom-candidate@sha256:" + "a" * 64 in command
    assert command[-1] == "/runtime/loom-candidate-runner"
    assert all(".env" not in item for item in command)


def test_podman_proposer_profile_allows_only_workspace_write_and_fixed_runner(tmp_path):
    for name in ("candidate", "workspace", "runtime"):
        (tmp_path / name).mkdir()
    seccomp = tmp_path / "seccomp.json"
    seccomp.write_text("{}", encoding="utf-8")
    runtime = PodmanRootlessRuntime(
        image="loom-proposer@sha256:" + "b" * 64,
        seccomp_profile=seccomp,
    )
    spec = SandboxSpec(
        "/candidate",
        "/workspace",
        "/runtime",
        "/scratch",
        256,
        30,
        16,
        64,
        4096,
        purpose="proposer",
        workspace_writable=True,
    )

    command = runtime.build_command(spec, _mounts(tmp_path))
    workspace_mount = next(item for item in command if "destination=/workspace" in item)

    assert "ro=true" not in workspace_mount
    assert next(item for item in command if "destination=/candidate" in item).endswith("ro=true,relabel=private")
    assert command[-1] == "/runtime/loom-proposer-runner"


def test_podman_runtime_requires_digest_pinned_image_and_seccomp_profile(tmp_path):
    seccomp = tmp_path / "seccomp.json"
    seccomp.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        PodmanRootlessRuntime(image="loom-candidate:latest", seccomp_profile=seccomp)
    with pytest.raises(ValueError):
        PodmanRootlessRuntime(
            image="loom-candidate@sha256:" + "a" * 64,
            seccomp_profile=tmp_path / "missing.json",
        )


def test_sandbox_mounts_reject_workspace_snapshots_containing_secrets(tmp_path):
    for name in ("candidate", "workspace", "runtime"):
        (tmp_path / name).mkdir()
    (tmp_path / "workspace" / ".env").write_text("API_KEY=secret", encoding="utf-8")

    with pytest.raises(ValueError, match="sanitized"):
        _mounts(tmp_path).resolved()


def test_sandbox_mounts_reject_nested_credentials_and_internal_symlinks(tmp_path):
    for name in ("candidate", "workspace", "runtime"):
        (tmp_path / name).mkdir()
    nested = tmp_path / "workspace" / "project" / ".aws"
    nested.mkdir(parents=True)
    (nested / "credentials").write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="sanitized"):
        _mounts(tmp_path).resolved()

    (nested / "credentials").unlink()
    nested.rmdir()
    (tmp_path / "candidate" / "escape").symlink_to(tmp_path / "workspace", target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        _mounts(tmp_path).resolved()
