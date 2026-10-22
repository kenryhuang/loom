"""Small, local execution-host facts for command-generating prompts.

No subprocesses, network requests, hostnames or credentials are collected here.
Docker CLI discovery deliberately says nothing about daemon access.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

DEFAULT_SHELL_ARGV = ("/bin/bash", "-o", "pipefail", "-c")


def _read_marker(path: str) -> str:
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            return stream.read(8192)
    except OSError:
        return ""


def _container() -> str:
    def marker_exists(path):
        try:
            return Path(path).exists()
        except OSError:
            return False

    if marker_exists("/.dockerenv"):
        return "docker"
    if marker_exists("/run/.containerenv"):
        return "container (Podman/OCI marker)"
    groups = (_read_marker("/proc/1/cgroup") + _read_marker("/proc/self/cgroup")).lower()
    if "docker" in groups:
        return "docker"
    if any(marker in groups for marker in ("kubepods", "containerd", "libpod", "lxc")):
        return "container (cgroup marker)"
    return "not detected (absence of markers is not proof of a bare host)"


def inspect_execution_environment(shell_argv=DEFAULT_SHELL_ARGV) -> dict:
    system = platform.system()
    name = "macOS" if system == "Darwin" else system or "unknown"
    version = platform.release()
    if system == "Darwin":
        version = platform.mac_ver()[0] or version
    elif system == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        name = release.get("PRETTY_NAME") or name
    return {
        "runtime": "native_os",
        "os": system or "unknown",
        "os_name": name,
        "os_version": version,
        "architecture": platform.machine() or "unknown",
        "shell_argv": list(shell_argv),
        "login_shell": os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown",
        "container": _container(),
        "docker_cli_available": shutil.which("docker") is not None,
        "docker_daemon": "not probed; verify access before using Docker",
    }


def format_execution_environment(environment) -> str:
    lines = ["Execution host environment:", json.dumps(dict(environment), ensure_ascii=False),
             "These facts describe the tool execution environment, including the OS inside a container, not the browser/client or physical Docker host.",
             "Use shell_argv for shell_execute syntax and quoting; login_shell is informational only. "
             "process_execute takes literal argv without shell expansion.",
             "Match commands, paths and utility flags to this OS and shell. Docker CLI presence does not establish daemon availability; "
             "do not assume commands run inside Docker unless container detection says so."]
    system = environment.get("os")
    if system == "Darwin":
        lines.append("macOS uses BSD utilities by default: avoid assuming GNU-only flags (e.g. sed -i requires a backup suffix on BSD sed).")
    elif system == "Linux":
        lines.append("Linux utility implementations vary (GNU/BusyBox); check availability and flags before relying on them.")
    elif system == "Windows":
        lines.append("Use the configured shell's syntax (PowerShell/cmd/Bash as configured), including its quoting and path rules; do not assume POSIX tools.")
    return "\n".join(lines)
