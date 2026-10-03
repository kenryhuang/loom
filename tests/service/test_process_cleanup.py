import contextlib
import os
import signal
import subprocess
import sys
import time

from loom.service.workers import process_identity, reap_group


def test_cleanup_reaps_group_after_leader_exit(tmp_path):
    marker = tmp_path / "orphan"
    child = f"import time; time.sleep(.7); open({str(marker)!r},'w').write('bad'); time.sleep(3)"
    leader = (
        "import subprocess,time; subprocess.Popen("
        + repr([sys.executable, "-c", child])
        + ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print('ready',flush=True); time.sleep(.2)"
    )
    process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        identity = process_identity(process.pid)
        process.wait(timeout=2)
        reap_group(process.pid, identity)
        time.sleep(0.9)
        assert not marker.exists()
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
