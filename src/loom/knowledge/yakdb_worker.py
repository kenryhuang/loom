"""Isolated adapter to the installed YakDB package; no vendored engine code."""

import asyncio
import json
import re
import sys
from pathlib import Path


async def execute(value):
    from yakdb import YakDB
    from yakdb_core.config import settings
    from yakdb_core.storage import get_backend

    workspace = YakDB.open(value["workspace"])
    await workspace.init()
    settings.vision_enabled = False  # Importing a file must not implicitly invoke a remote vision model.
    try:
        backend = get_backend()
        if value["action"] == "index":
            import magic
            from yakdb_core.services.ingest_service import ingest_file

            previous = await backend.find_file_exact(value["name"])
            if previous:
                await backend.delete_file(previous)
            data = Path(value["upload"]).read_bytes()
            mime = magic.from_buffer(data, mime=True)
            if value["name"].lower().endswith(".pdf"):
                mime = "application/pdf"
            result = await ingest_file(data, value["name"], mime)
            record = await backend.get_file_text(str(result["id"]))
            if not record or not re.sub(r"(?m)^\[(?:Page|Slide|Sheet|Section) [^\n]*\]\s*$", "", record.get("full_text", "")).strip():
                raise ValueError("Document contains no readable text; check OCR installation and language packs")
            count = await backend.get_total_pages(str(result["id"]))
            return {"pages": count}
        if value["action"] == "delete":
            fid = await backend.find_file_exact(value["name"])
            if fid:
                await backend.delete_file(fid)
            return {}
        result = await workspace.search(value["query"], limit=value["limit"])
        for hit in result["results"]:
            passage = await workspace.read(hit["file_id"], pages=str(hit["page_number"]))
            hit["text"] = passage[:12000]
            hit["truncated"] = len(passage) > 12000
        return result
    finally:
        await workspace.close()


def main():
    # Third-party parsers may print diagnostics; keep stdout a strict JSON protocol.
    import contextlib

    value = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(execute(value))
        print(json.dumps({"result": result}))
    except Exception as exc:
        print(json.dumps({"error": f"YakDB {value['action']} failed ({type(exc).__name__}). Check document format and local parser/OCR dependencies."}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
