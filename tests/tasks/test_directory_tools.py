import asyncio

from loom.core import thaw_json
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from loom.tasks.tools import make_task_tools


def test_read_only_workspace_supports_directory_discovery(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')")
    (tmp_path / "README.md").write_text("Project")
    spec = {"session_environment": {"plugin": "session", "resources": [
        {"id": "workspace", "kind": "directory", "uri": str(tmp_path), "access": "read-only"}]},
        "tools": {"collections": ["filesystem", "shell", "task_control"]}}
    request = TaskRequest("Inspect", workspace=tmp_path, task_spec=spec)
    assembly = TaskAssembly(request)
    assert {"list_directory", "read_file", "finish"} <= set(assembly.bindings)
    assert not {"shell_execute", "process_execute", "write_file", "edit_file"} & set(assembly.bindings)
    assert assembly.bindings["list_directory"].effect_kind == "read_only"
    tool = make_task_tools(request)["list_directory"]
    first = asyncio.run(tool({"limit": 1})).unwrap()
    assert thaw_json(first.value) == {"path": ".", "entries": [{"path": "README.md", "kind": "file"}], "next_offset": 1}
    second = asyncio.run(tool({"offset": first.value["next_offset"], "limit": 1})).unwrap()
    assert thaw_json(second.value) == {"path": ".", "entries": [{"path": "src", "kind": "directory"}], "next_offset": None}
    nested = asyncio.run(tool({"path": "src"})).unwrap()
    assert thaw_json(nested.value["entries"]) == [{"path": "src/app.py", "kind": "file"}]


def test_directory_discovery_cannot_traverse_outside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "private.txt").write_text("outside")
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    (root / "broken").symlink_to(tmp_path / "missing")
    tool = make_task_tools(TaskRequest("Inspect", workspace=root))["list_directory"]
    entries = asyncio.run(tool({})).unwrap().value["entries"]
    assert thaw_json(entries) == [{"path": "broken", "kind": "symlink"}, {"path": "outside", "kind": "symlink"}]
    for path in ("..", "outside", str(tmp_path)):
        result = asyncio.run(tool({"path": path}))
        assert not result.ok and result.error.code == "VALIDATION_FAILED"


def test_directory_discovery_reports_invalid_input_and_missing_paths(tmp_path):
    (tmp_path / "file").write_text("text")
    tool = make_task_tools(TaskRequest("Inspect", workspace=tmp_path))["list_directory"]
    for payload in ({"offset": -1}, {"limit": 0}, {"limit": 1001}, {"offset": True}, {"limit": "2"}):
        result = asyncio.run(tool(payload))
        assert not result.ok and result.error.code == "VALIDATION_FAILED"
    for path in ("missing", "file"):
        result = asyncio.run(tool({"path": path}))
        assert not result.ok and result.error.code == "TOOL_FAILED"
