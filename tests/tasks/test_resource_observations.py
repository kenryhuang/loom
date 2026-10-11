import asyncio
import hashlib

from loom.tasks.request import TaskRequest
from loom.tasks.tools import make_task_tools


def test_filesystem_observations_record_actual_versions_and_ranges(tmp_path):
    tools = make_task_tools(TaskRequest("Inspect", workspace=tmp_path))

    async def run():
        written = (await tools["write_file"]({"path": "a.txt", "content": "abcdef"})).unwrap().value
        read = (await tools["read_file"]({"path": "./a.txt", "max_bytes": 3})).unwrap().value
        edited = (await tools["edit_file"]({"path": "a.txt", "edits": [{"old_text": "abc", "new_text": "xyz"}]})).unwrap().value
        return written, read, edited

    written, read, edited = asyncio.run(run())
    assert written["resource"]["before_state"] == "absent"
    assert read["resource"]["path"] == str(tmp_path / "a.txt")
    assert tuple(read["resource"]["byte_range"]) == (0, 3)
    assert read["resource"]["returned_sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert read["truncated"] is True
    assert read["resource"]["after_sha256"] == written["resource"]["after_sha256"] == edited["resource"]["before_sha256"]
    assert edited["resource"]["after_sha256"] == hashlib.sha256((tmp_path / "a.txt").read_bytes()).hexdigest()


def test_noop_write_keeps_same_version_and_shell_records_resolved_directory(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    tools = make_task_tools(TaskRequest("Inspect", workspace=tmp_path))

    async def run():
        write = (await tools["write_file"]({"path": "a.txt", "content": "same"})).unwrap().value
        shell = (await tools["shell_execute"]({"command": "cat a.txt"})).unwrap().value
        return write, shell

    write, shell = asyncio.run(run())
    assert write["resource"]["before_sha256"] == write["resource"]["after_sha256"]
    assert shell["resolved_cwd"] == str(tmp_path)
    assert shell["stdout"] == "same"
