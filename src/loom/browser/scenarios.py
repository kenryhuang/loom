"""Bounded browser contracts shared by execution tools and independent verification."""

import json
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

STRING = {"type": "string", "minLength": 1, "maxLength": 2000}
SELECTOR = {"type": "string", "minLength": 1, "maxLength": 500}
STEP = {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
    "action": {"enum": ["click", "fill", "select", "press", "scroll", "drag", "wait_visible", "wait_text", "adapter"]},
    "selector": SELECTOR, "value": STRING, "target": SELECTOR,
    "x": {"type": "integer", "minimum": -5000, "maximum": 5000}, "y": {"type": "integer", "minimum": -5000, "maximum": 5000},
}}
ASSERTION = {"type": "object", "additionalProperties": False, "required": ["id", "kind", "selector"], "properties": {
    "id": STRING, "selector": SELECTOR, "kind": {"enum": ["count", "text", "visible", "hidden"]},
    "expected": {"type": ["string", "integer", "boolean"]},
}}
SCENARIO_SCHEMA = {"type": "object", "additionalProperties": False, "anyOf": [
    {"required": ["url"], "properties": {"url": STRING, "preview": {"type": "null"}}},
    {"required": ["preview"], "properties": {"preview": {"type": "object"}, "url": {"type": "null"}}},
], "properties": {
    "id": STRING, "url": {**STRING, "description": "Explicitly bound external URL. Omit or set null when using preview; never provide both."},
    "preview": {"type": "object", "additionalProperties": False, "required": ["argv"], "properties": {
        "argv": {"type": "array", "minItems": 1, "maxItems": 24, "items": STRING,
                 "description": "Actual preview command with {port}, e.g. [npm, run, dev, --, --host, 127.0.0.1, --port, {port}]. No shell syntax."},
        "cwd": STRING, "route": STRING,
    }, "description": "Version-bound local preview. For workspace code choose this and omit url (or set url null)."},
    "viewport": {"type": "object", "additionalProperties": False, "properties": {
        "width": {"type": "integer", "minimum": 320, "maximum": 2560},
        "height": {"type": "integer", "minimum": 240, "maximum": 1600},
    }},
    "steps": {"type": "array", "maxItems": 12, "items": STEP},
    "ready_selector": SELECTOR, "canvas": {"type": "boolean"},
    "adapter_path": {**STRING, "description": "Optional existing browser adapter function file, not an application entry point. "
                     "Omit or set null unless verified available."},
    "captures": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "object", "additionalProperties": False,
        "properties": {"id": STRING, "selector": SELECTOR, "after_ms": {"type": "integer", "minimum": 0, "maximum": 5000}}}},
    "assertions": {"type": "array", "maxItems": 16, "items": ASSERTION},
}}
OBSERVATION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["page_id"], "properties": {
    "page_id": STRING, **{k: SCENARIO_SCHEMA["properties"][k] for k in ("ready_selector", "canvas", "captures", "assertions")},
}}


def normalize_scenario(value, *, path=""):
    """Record removal of native nullable fields before local schema validation."""
    changes = []

    def clean(node, current):
        if isinstance(node, dict):
            result = {}
            for key, child in node.items():
                child_path = current + "/" + key
                if child is None:
                    changes.append({"path": child_path, "rule": "native_optional_null"})
                else:
                    result[key] = clean(child, child_path)
            return result
        return [clean(v, current + "/" + str(i)) for i, v in enumerate(node)] if isinstance(node, list) else node

    return clean(value, path), changes


def validate_scenario(value):
    value, _ = normalize_scenario(value)
    if len(json.dumps(value)) > 16000:
        raise ValueError("Browser scenario exceeds size limit")
    if isinstance(value, dict):
        if not value.get("url") and not value.get("preview"):
            raise ValueError("Browser scenario requires url or preview")
        if value.get("url") and value.get("preview"):
            raise ValueError("Choose exactly one target: for workspace code use preview and omit url (native JSON Schema: url=null)")
    error = next(Draft202012Validator(SCENARIO_SCHEMA).iter_errors(value), None)
    if error:
        raise ValueError(f"Invalid browser scenario at {'/'.join(map(str, error.path))}: {error.message}")
    if value.get("preview") and not any("{port}" in arg for arg in value["preview"]["argv"]):
        raise ValueError("preview.argv must include {port}; e.g. npm run dev -- --host 127.0.0.1 --port {port}")
    for step in value.get("steps", []):
        if step["action"] != "adapter" and not step.get("selector") and step["action"] != "scroll":
            raise ValueError("Browser step requires selector")
        if step["action"] in {"fill", "select", "press", "wait_text", "adapter"} and not step.get("value"):
            raise ValueError("Browser step requires value")
        if step["action"] == "drag" and not step.get("target"):
            raise ValueError("Drag requires a target selector")
    for row in value.get("assertions", []):
        expected, kind = row.get("expected"), row["kind"]
        if kind == "count" and (type(expected) is not int or expected < 0) or kind == "text" and not isinstance(expected, str):
            raise ValueError("Browser count/text assertion requires a typed expected value")
    ids = [a["id"] for a in value.get("assertions", [])]
    captures = value.get("captures", [{"id": "viewport"}])
    capture_ids = [c.get("id", f"frame-{i}") for i, c in enumerate(captures)]
    if len(set(ids)) != len(ids) or len(set(capture_ids)) != len(capture_ids):
        raise ValueError("Browser assertion and capture IDs must be unique")
    return value


def origin(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Browser targets must be credential-free HTTP(S) URLs")
    return f"{p.scheme}://{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}"
