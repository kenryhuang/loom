"""Replaceable asset host and UI catalog; execution stays in the service API."""

from copy import deepcopy
from importlib.resources import files

from loom.service.contracts import COMMAND_TYPES

COMMAND_LABELS = {
    "pause": "Pause", "resume": "Resume", "stop_run": "Stop run",
    "complete_task": "Complete task", "reopen_task": "Reopen task",
}


class WebFrontend:
    """Hosts package resources. Subclasses can supply a different UI/catalog."""

    def __init__(self, *, package="loom.web", templates=None):
        self.assets = files(package).joinpath("assets")
        self.templates = templates if templates is not None else builtin_templates()

    def asset(self, path):
        if path in {"/", "/web", "/web/"}:
            relative, content_type = "index.html", "text/html; charset=utf-8"
        elif path.startswith("/web/assets/"):
            relative = path.removeprefix("/web/assets/")
            parts = relative.split("/")
            if any(part in {"", ".", ".."} or "\\" in part for part in parts):
                return None
            suffix = relative.rsplit(".", 1)[-1]
            content_type = {"mjs": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}.get(suffix)
            if content_type is None:
                return None
        else:
            return None
        resource = self.assets.joinpath(relative)
        return (resource.read_bytes(), content_type) if resource.is_file() else None

    def catalog(self, service):
        models, default_model = [], None
        if service.config_path:
            from loom.tasks.config import load_task_config

            loaded = load_task_config(service.config_path)
            if loaded.ok:
                default_model = loaded.value.default_model
                models = [{"id": name, "label": f"{name} · {value.model}"} for name, value in loaded.value.models.items()]
        return {
            "schema_version": 1,
            "commands": [{"id": name, "label": COMMAND_LABELS[name]} for name in COMMAND_LABELS if name in COMMAND_TYPES],
            "templates": deepcopy(self.templates),
            "models": models,
            "default_model": default_model,
        }


def builtin_templates():
    general = {
        "session_environment": {"plugin": "session"},
        "context": {"plugin": "bounded_context", "max_window_chars": 24000},
        "tools": {"collections": ["task_control"]},
        "workflow": {"plugin": "dynamic"},
    }
    research = deepcopy(general)
    research["context"]["plugin"] = "research_context"
    research["tools"]["collections"] = ["web_research", "document_outputs", "task_control"]
    research["outputs"] = [{"kind": "report", "format": "markdown", "require_evidence_refs": True, "require_verified_sources": True}]
    return [
        {"id": "general", "label": "General", "description": "Explain, reason, and write", "task_spec": general},
        {"id": "research", "label": "Research", "description": "Read web sources and write cited reports", "task_spec": research},
        {"id": "coding", "label": "Coding", "description": "Work with files and commands", "requires_workspace": True},
    ]
