"""Integration tests against the separately installed engine, not a fake YakDB."""

import base64
from concurrent.futures import ThreadPoolExecutor

import pytest

from loom.knowledge.store import KnowledgeStore
from loom.service.contracts import ServiceError

pytest.importorskip("yakdb")
fitz = pytest.importorskip("fitz")


def pdf_payload(name="manual.pdf"):
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Introduction to orbital operations. " * 3)
        pdf.new_page().insert_text((72, 72), "Moonbase opens July eight. " * 4)
        return {"name": name, "file_base64": base64.b64encode(pdf.tobytes()).decode()}


def test_pdf_native_search_page_citations_replacement_failure_and_delete(tmp_path):
    store = KnowledgeStore(tmp_path)
    base = store.create({"name": "PDF library", "engine": "yakdb_local"})
    result = store.index(base["id"], pdf_payload())
    assert result["chunks"] == 2
    hits = store.search([base["id"]], "Moonbase")["matches"]
    assert len(hits) == 1 and hits[0]["page_number"] == 2
    assert "July eight" in hits[0]["text"]
    assert hits[0]["source_id"].endswith("#page=2")
    generation = store.get(base["id"])["yakdb_generation"]
    with pytest.raises(ServiceError, match="YakDB index failed"):
        store.index(base["id"], {"name": "manual.pdf", "file_base64": base64.b64encode(b"not a PDF").decode()})
    assert store.get(base["id"])["yakdb_generation"] == generation
    assert store.search([base["id"]], "Moonbase")["matches"]
    replacement = pdf_payload()
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Sunbase revised operations schedule. " * 3)
        replacement["file_base64"] = base64.b64encode(pdf.tobytes()).decode()
    changed = store.index(base["id"], replacement)
    assert changed["document_id"] == result["document_id"]
    assert not store.search([base["id"]], "Moonbase")["matches"]
    assert store.search([base["id"]], "Sunbase")["matches"]
    reopened = KnowledgeStore(tmp_path)
    assert reopened.get(base["id"])["chunk_count"] == 1
    reopened.remove_document(base["id"], result["document_id"])
    assert not reopened.search([base["id"]], "Sunbase")["matches"]
    assert reopened.get(base["id"])["documents"] == []


def test_concurrent_libraries_are_isolated_and_duplicate_import_is_explicit(tmp_path):
    store = KnowledgeStore(tmp_path)
    bases = [store.create({"name": name, "engine": "yakdb_local"}) for name in ["One", "Two"]]
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda pair: store.index(pair[0]["id"], {"name": "same.txt", "content": pair[1]}), zip(bases, ["Uniquealpha", "Uniquebeta"], strict=True))
        )
    assert len(results) == 2
    assert store.search([bases[0]["id"]], "Uniquealpha")["matches"]
    assert not store.search([bases[1]["id"]], "Uniquealpha")["matches"]
    with pytest.raises(ServiceError, match="already indexed"):
        store.index(bases[0]["id"], {"name": "another.txt", "content": "Uniquealpha"})
    with pytest.raises(ServiceError, match="base64"):
        store.index(bases[0]["id"], {"name": "bad.pdf", "file_base64": "$$$"})


def test_blank_pdf_is_not_published(tmp_path):
    store = KnowledgeStore(tmp_path)
    base = store.create({"name": "Empty", "engine": "yakdb_local"})
    with fitz.open() as pdf:
        pdf.new_page()
        payload = {"name": "blank.pdf", "file_base64": base64.b64encode(pdf.tobytes()).decode()}
    with pytest.raises(ServiceError, match="YakDB index failed"):
        store.index(base["id"], payload)
    assert store.get(base["id"])["documents"] == []


def test_english_scanned_pdf_with_installed_tesseract(tmp_path):
    import shutil

    if not shutil.which("tesseract"):
        pytest.skip("Tesseract is an optional system dependency")
    store = KnowledgeStore(tmp_path)
    base = store.create({"name": "Scanned", "engine": "yakdb_local"})
    with fitz.open() as source:
        page = source.new_page()
        page.insert_text((72, 100), "Orbital launch schedule", fontsize=24)
        raster = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_image(page.rect, stream=raster)
        payload = {"name": "scan.pdf", "file_base64": base64.b64encode(pdf.tobytes()).decode()}
    store.index(base["id"], payload)
    assert store.search([base["id"]], "Orbital")["matches"][0]["page_number"] == 1
