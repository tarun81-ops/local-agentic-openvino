"""Scanned-PDF OCR fallback and optional image captions (no OCR engine or model needed)."""
from types import SimpleNamespace

import pymupdf

from app.config import settings
from app.core.parsers import image_parser, pdf_parser


def _pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_scanned_page_is_ocrd_and_keeps_its_page_number(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    _pdf(pdf, ["This page has a real text layer with enough words.", ""])
    seen = []
    monkeypatch.setattr(image_parser, "available", lambda: True)
    monkeypatch.setattr(image_parser, "ocr", lambda img: (seen.append(img[:4]), "Receipt total 99.50 Zenith Ltd")[1])
    chunks = pdf_parser.parse_pdf(pdf)
    assert [(c.loc_kind, c.loc_no) for c in chunks] == [("page", 1), ("page", 2)]
    assert "Zenith" in chunks[1].text
    assert seen == [b"\x89PNG"]  # only the blank page was rendered and OCR'd


def test_without_ocr_blank_pages_are_skipped(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    _pdf(pdf, [""])
    monkeypatch.setattr(image_parser, "available", lambda: False)
    assert pdf_parser.parse_pdf(pdf) == []


def test_caption_is_indexed_when_enabled(tmp_path, monkeypatch):
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(settings, "caption_images", True)
    monkeypatch.setattr(image_parser, "available", lambda: False)
    monkeypatch.setattr(image_parser, "caption", lambda p: "A whiteboard listing the Q3 launch dates.")
    chunks = image_parser.parse_image(img)
    assert chunks and chunks[0].loc_kind == "image" and "whiteboard" in chunks[0].text


def test_caption_failure_does_not_fail_indexing(tmp_path, monkeypatch):
    img = tmp_path / "photo.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(settings, "caption_images", True)
    monkeypatch.setattr(image_parser, "available", lambda: True)
    monkeypatch.setattr(image_parser, "ocr", lambda p: "STOP sign")

    def boom(p):
        raise ConnectionError("model down")

    monkeypatch.setattr(image_parser, "caption", boom)
    assert image_parser.parse_image(img)[0].text == "STOP sign"


def test_caption_sends_the_image_to_the_model(tmp_path, monkeypatch):
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"jpegbytes")
    sent = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    sent.update(kw)
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" A cat. "))])

    import app.core.llm.client as client

    monkeypatch.setattr(client, "get_client", lambda local: FakeClient)  # captions must ask for the local client
    assert image_parser.caption(img) == "A cat."
    url = sent["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
