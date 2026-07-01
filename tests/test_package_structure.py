import ast
from pathlib import Path


def test_subpackage_init_files_are_export_shims():
    package_root = Path(__file__).resolve().parents[1] / "src" / "loom"
    offenders = []

    for init_file in sorted(package_root.glob("*/__init__.py")):
        tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
        definitions = [node.name for node in tree.body if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)]
        if definitions:
            offenders.append(f"{init_file.relative_to(package_root)}: {', '.join(definitions)}")

    assert offenders == []


def test_init_files_use_explicit_exports():
    package_root = Path(__file__).resolve().parents[1] / "src" / "loom"
    offenders = []

    for init_file in sorted(package_root.rglob("__init__.py")):
        tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if any(alias.name == "*" for alias in node.names):
                offenders.append(f"{init_file.relative_to(package_root)} uses wildcard import")
            if any(alias.name == "__all__" for alias in node.names):
                offenders.append(f"{init_file.relative_to(package_root)} imports __all__")

    assert offenders == []


def test_top_level_package_api_is_intentionally_thin():
    import loom

    public_names = {name for name in dir(loom) if not name.startswith("_")}
    submodule_names = {"composition", "core", "evolution", "examples", "llm", "observability", "runtime", "tasks", "tools", "tui"}

    assert public_names - submodule_names == set()
    assert loom.__version__ == "0.1.0"
    assert not hasattr(loom, "Context")
