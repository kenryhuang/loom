import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from loom import host_environment as environment
from loom.host_environment import _read_marker
from loom.llm import build_messages, build_system_prompt
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment.platform, "release", lambda: "25.0")
    monkeypatch.setattr(environment.platform, "mac_ver", lambda: ("26.0", (), "arm64"))
    monkeypatch.setattr(environment.platform, "machine", lambda: "arm64")
    original_exists = environment.Path.exists
    monkeypatch.setattr(environment.Path, "exists", lambda self: False if str(self) in {"/.dockerenv", "/run/.containerenv"} else original_exists(self))
    monkeypatch.setattr(environment, "_read_marker", lambda path: "")
    monkeypatch.setattr(environment.shutil, "which", lambda command: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")


def test_macos_prompt_uses_execution_shell_instead_of_login_shell(host, tmp_path):
    context = make_task_context(TaskRequest("Fix this project", workspace=tmp_path)).unwrap()
    facts = context.metadata["execution_environment"]
    assert facts["os_name"] == "macOS"
    assert facts["os_version"] == "26.0"
    assert facts["architecture"] == "arm64"
    assert facts["shell_argv"] == environment.DEFAULT_SHELL_ARGV
    assert facts["login_shell"] == "/bin/zsh"
    prompt = build_messages(context)[0].content
    assert "BSD utilities" in prompt
    assert "login_shell is informational only" in prompt
    assert '"docker_cli_available": false' in prompt
    assert "not detected" in prompt


@pytest.mark.parametrize("marker,cgroup,expected", [
    ("/.dockerenv", "", "docker"),
    (None, "0::/docker/abc", "docker"),
    ("/run/.containerenv", "", "container (Podman/OCI marker)"),
    (None, "0::/kubepods/pod/abc", "container (cgroup marker)"),
    (None, "0::/", "not detected"),
])
def test_linux_container_and_cli_are_independent(host, monkeypatch, marker, cgroup, expected):
    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.platform, "freedesktop_os_release", lambda: {"PRETTY_NAME": "Alpine Linux v3.22"})
    monkeypatch.setattr(environment.Path, "exists", lambda self: str(self) == marker)
    monkeypatch.setattr(environment, "_read_marker", lambda path: cgroup)
    monkeypatch.setattr(environment.shutil, "which", lambda command: "/usr/bin/docker")
    facts = environment.inspect_execution_environment()
    assert facts["os_name"] == "Alpine Linux v3.22"
    assert facts["container"].startswith(expected)
    assert facts["docker_cli_available"] is True
    assert facts["docker_daemon"].startswith("not probed")
    assert "GNU/BusyBox" in environment.format_execution_environment(facts)


def test_unreadable_container_markers_and_missing_os_release_are_tolerated(host, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.platform, "freedesktop_os_release", denied)
    monkeypatch.setattr(environment.Path, "open", denied)
    monkeypatch.setattr(environment.Path, "exists", denied)
    # Use the real bounded reader rather than the fixture's fake.
    assert _read_marker("/proc/1/cgroup") == ""
    facts = environment.inspect_execution_environment()
    assert facts["os_name"] == "Linux"
    assert facts["container"].startswith("not detected")


def test_windows_prompt_uses_configured_powershell(host, monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    shell = ["pwsh", "-NoProfile", "-Command"]
    facts = environment.inspect_execution_environment(shell)
    assert facts["shell_argv"] == shell
    prompt = environment.format_execution_environment(facts)
    assert '"pwsh", "-NoProfile", "-Command"' in prompt
    assert "path rules" in prompt
    assert "BSD utilities" not in prompt


def test_unreported_runtime_does_not_claim_local_host_facts(host):
    assembly = TaskAssembly(TaskRequest("Explain", task_spec={}))
    native = assembly.runtime
    assembly.runtime = SimpleNamespace(manifest=SimpleNamespace(plugin_id="remote"))
    facts = assembly.execution_environment()
    assert facts["runtime"] == "remote"
    assert facts["os"] == "unknown"
    assert facts["shell_argv"] is None
    assert "macOS" not in environment.format_execution_environment(facts)
    assembly.runtime = native
    asyncio.run(assembly.close())


def test_runtime_restore_refreshes_host_and_preserves_configured_shell(host, monkeypatch, tmp_path):
    request = TaskRequest("Fix", workspace=tmp_path, task_spec={
        "session_environment": {"plugin": "session", "resources": [
            {"id": "workspace", "kind": "directory", "uri": str(tmp_path), "access": "read-write"},
        ]},
        "execution_runtime": {"plugin": "native_os", "shell": ["/bin/sh", "-c"]},
    })
    first = TaskAssembly(request)
    old = make_task_context(request, assembly=first).unwrap()
    snapshot = first.snapshot()
    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.platform, "freedesktop_os_release", lambda: {"PRETTY_NAME": "Debian GNU/Linux 13"})
    restored = TaskAssembly(request)
    restored.restore(snapshot)
    projected = restored.project_acceptance(old)
    assert old.metadata["execution_environment"]["os"] == "Darwin"
    assert projected.metadata["execution_environment"]["os"] == "Linux"
    prompt = restored.context_manager.project(projected)[0].content
    assert '"shell_argv": ["/bin/sh", "-c"]' in prompt
    assert "Debian GNU/Linux 13" in prompt
    assert "BSD utilities" not in prompt
    asyncio.run(first.close())
    asyncio.run(restored.close())


def test_legacy_context_without_environment_gets_current_host(host, tmp_path):
    context = make_task_context(TaskRequest("Explain", workspace=tmp_path)).unwrap()
    context = replace(context, metadata={})
    assert '"os_name": "macOS"' in build_system_prompt(context)


def test_acceptance_command_planner_receives_host_in_system_prompt(host):
    assembly = TaskAssembly(TaskRequest("Explain", task_spec={}), plan_mode="off")
    assembly.configure_acceptance(SimpleNamespace(model="fixture"))
    captured = []

    async def request(runtime, stage, prompt, text, seconds, **options):
        from loom.llm import LlmResponse

        captured.append(prompt)
        return LlmResponse(content=json.dumps({"criteria": []}))

    assembly.acceptance_model_request = request
    asyncio.run(assembly.acceptance.model(None, "plan", "You design acceptance.", {}))
    assert "Execution host environment:" in captured[0]
    assert "BSD utilities" in captured[0]
    asyncio.run(assembly.close())
