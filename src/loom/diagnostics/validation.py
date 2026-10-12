"""Bounded, offline test execution with OS-enforced write isolation."""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from loom.core import now_iso
from loom.diagnostics.evidence import redact


def backend():
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap"
    return None


def sandbox(command, workspace, scratch):
    system = [Path(p) for p in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/System", "/Library", "/opt") if Path(p).exists()]
    runtime = [Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).resolve().parent.parent]
    reads = list(dict.fromkeys(str(p.resolve()) for p in [*system, *runtime, workspace, scratch]))
    if backend() == "sandbox-exec":

        def quoted(value):
            return json.dumps(str(value))

        profile = (
            "(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow signal (target same-sandbox))"
            '(allow file-read-metadata)(allow file-read* (literal "/") (subpath "/dev") (subpath "/private/etc"))'
            '(allow file-write* (literal "/dev/null") (literal "/dev/tty"))'
            + "(allow file-read* "
            + " ".join("(subpath " + quoted(p) + ")" for p in reads)
            + ")"
            + "(allow file-write* (subpath "
            + quoted(workspace)
            + ") (subpath "
            + quoted(scratch)
            + "))"
            # Git metadata is host-owned even though .git lives inside the worktree.
            + "(deny file-read* file-write* (literal "
            + quoted(workspace / ".git")
            + "))"
        )
        return ["/usr/bin/sandbox-exec", "-p", profile, *command]
    if backend() == "bubblewrap":
        args = [shutil.which("bwrap"), "--die-with-parent", "--unshare-all", "--new-session", "--proc", "/proc", "--dev", "/dev"]
        for p in list(dict.fromkeys([*system, *runtime])):
            args += ["--ro-bind", str(p), str(p)]
        args += [
            "--bind",
            str(workspace),
            str(workspace),
            "--bind",
            str(scratch),
            str(scratch),
            "--ro-bind",
            "/dev/null",
            str(workspace / ".git"),
            "--chdir",
            str(workspace),
            "--",
            *command,
        ]
        return args
    raise ValueError("Validation requires macOS sandbox-exec or Linux bubblewrap; unrestricted execution is disabled")


async def run_check(workspace, argv, timeout=60):
    if not isinstance(argv, list) or not argv or len(argv) > 100 or any(not isinstance(a, str) or len(a) > 2000 for a in argv):
        raise ValueError("argv must be a nonempty array of strings")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("Validation timeout must be between 1 and 120 seconds")
    if argv[0] in {"python", "python3", "pytest"}:
        command = [sys.executable, *(["-m", "pytest"] if argv[0] == "pytest" else []), *argv[1:]]
    elif argv[0] == "node" and shutil.which("node"):
        command = [shutil.which("node"), *argv[1:]]
    else:
        raise ValueError("Validation supports python, pytest and node; no shell, installation or network access")
    scratch = workspace.directory / "scratch"
    scratch.mkdir(exist_ok=True, mode=0o700)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PYTHONPATH": str(workspace.path / "src") + os.pathsep + str(workspace.path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "LANG": "en_US.UTF-8",
    }
    patch, _ = workspace.patch()
    from loom.diagnostics.workspace import digest

    receipt = {
        "argv": argv,
        "started_at": now_iso(),
        "timeout_seconds": timeout,
        "sandbox": backend(),
        "baseline": workspace.meta["baseline"],
        "patch_sha256": digest(patch),
    }
    started = time.monotonic()
    # A recorded process group lets timeout, cancellation and recovery reap test descendants.
    process = await asyncio.create_subprocess_exec(
        *sandbox(command, workspace.path, scratch),
        cwd=workspace.path,
        env=env,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    from loom.service.workers import process_identity

    workspace.meta["validation_process"] = {"pid": process.pid, "identity": process_identity(process.pid)}
    workspace.save()
    output = bytearray()
    truncated = False

    async def consume():
        nonlocal truncated
        while chunk := await process.stdout.read(8192):
            room = max(0, 32000 - len(output))
            output.extend(chunk[:room])
            truncated |= len(chunk) > room
        await process.wait()

    try:
        await asyncio.wait_for(consume(), timeout)
        receipt["status"] = "passed" if process.returncode == 0 else "failed"
    except TimeoutError:
        receipt["status"] = "timeout"
    finally:
        await asyncio.to_thread(workspace.reap_validation)
        await process.wait()
    receipt.update(
        exit_code=process.returncode, seconds=round(time.monotonic() - started, 3), output=redact(output.decode(errors="replace")), output_truncated=truncated
    )
    workspace.meta["checks"].append(receipt)
    workspace.save()
    return receipt
