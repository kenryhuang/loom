"""Bounded model views of retained evidence; artifacts keep the original value."""

import json

PAGE_CHARS = 6000


def text_page(text, *, offset=0, limit=PAGE_CHARS):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > len(text):
        raise ValueError("offset must be a character position within the artifact")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_CHARS:
        raise ValueError(f"limit must be between 1 and {PAGE_CHARS} characters")
    end = min(len(text), offset + limit)
    return {
        "offset": offset,
        "total_chars": len(text),
        "has_more": end < len(text),
        "next_offset": end if end < len(text) else None,
    }, text[offset:end]


def source_preview(value):
    """Expose readable source text, without repeating HTML in the model window."""
    if "page" in value:
        return value
    field = "text" if isinstance(value.get("text"), str) else "content"
    if not isinstance(value.get(field), str):
        return value
    page, text = text_page(value[field])
    return {
        **{key: item for key, item in value.items() if key not in {"text", "content"}},
        "page": page,
        "read_more": {"digest": value["artifact"]["sha256"], "view": "text", "offset": page["next_offset"]} if page["has_more"] else None,
        field: text,
    }


def artifact_page(value, digest, *, view="auto", offset=0, limit=PAGE_CHARS):
    if view not in {"auto", "text", "json"}:
        raise ValueError("view must be auto, text, or json")
    text = value if isinstance(value, str) else None
    if isinstance(value, dict):
        text = value.get("text", value.get("content"))
    if view == "text" and not isinstance(text, str):
        raise ValueError("Artifact has no text field; use view=json")
    if view == "json" or not isinstance(text, str):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        view = "json"
    else:
        view = "text"
    page, content = text_page(text, offset=offset, limit=limit)
    return {
        "digest": digest,
        "view": view,
        **page,
        "read_more": {"digest": digest, "view": view, "offset": page["next_offset"], "limit": limit} if page["has_more"] else None,
        "content": content,
    }
