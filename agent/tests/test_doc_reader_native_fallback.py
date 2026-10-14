"""Physical PDF text survives missing OCR without hiding extraction limitations."""

import json

import pytest

from src.config.accessor import reset_env_config
from src.tools import doc_reader_tool

pdfium = pytest.importorskip("pypdfium2")
canvas = pytest.importorskip("reportlab.pdfgen.canvas")


@pytest.fixture(autouse=True)
def disable_ocr(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_TRADING_OCR_ENGINE", "none")
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_FILE_ROOTS", str(tmp_path))
    monkeypatch.setattr(doc_reader_tool, "_cached_ocr_checked", False)
    monkeypatch.setattr(doc_reader_tool, "_cached_ocr_engine", None)
    reset_env_config()
    yield
    reset_env_config()


def read_pdf(tmp_path, texts, **kwargs):
    path = tmp_path / "report.pdf"
    writer = canvas.Canvas(str(path))
    for text in texts:
        if text:
            writer.drawString(50, 750, text)
        writer.showPage()
    writer.save()
    # Independent native extractor verifies these are real text pages, rather
    # than OCR mocks or fixtures that accidentally contain only raster images.
    document = pdfium.PdfDocument(str(path))
    try:
        assert [
            document[i].get_textpage().get_text_range().strip()
            for i in range(len(document))
        ] == texts
    finally:
        document.close()
    return json.loads(
        doc_reader_tool.DocReaderTool().execute(file_path=str(path), **kwargs)
    )


@pytest.mark.parametrize(
    "texts,pages,threshold,expected,skipped",
    [
        (["Revenue: $42m"], "", 50, ["Revenue: $42m"], 0),
        (
            ["A native summary longer than fifty characters is ordinarily readable."],
            "",
            200,
            ["A native summary longer than fifty characters is ordinarily readable."],
            0,
        ),
        (
            [
                "Revenue: $42m",
                "A native summary longer than fifty characters is ordinarily readable.",
            ],
            "",
            50,
            [
                "Revenue: $42m",
                "A native summary longer than fifty characters is ordinarily readable.",
            ],
            0,
        ),
        (["Revenue: $42m", "Another short note"], "2", 50, ["Another short note"], 0),
        (["", "Revenue: $42m"], "", 50, ["Revenue: $42m"], 1),
    ],
)
def test_shipping_reader_retains_short_native_text(
    tmp_path, texts, pages, threshold, expected, skipped
):
    result = read_pdf(tmp_path, texts, pages=pages, min_text_per_page=threshold)
    assert result["status"] == "ok", result
    for text in expected:
        assert text in result["text"]
    assert result["skipped_pages"] == skipped
    assert result["ocr_pages"] == 0 and result["ocr_engine"] is None
    assert result["ocr_quality"]["quality_flag"] == "no_ocr_engine"
    if pages == "2":
        assert "Revenue: $42m" not in result["text"]


def test_long_native_page_keeps_no_ocr_needed_control(tmp_path):
    text = "A native summary longer than fifty characters is ordinarily readable."
    result = read_pdf(tmp_path, [text])
    assert result["status"] == "ok" and text in result["text"]
    assert result["skipped_pages"] == 0
    assert result["ocr_quality"]["quality_flag"] == "no_ocr_needed"


def test_blank_pdf_still_requires_ocr(tmp_path):
    result = read_pdf(tmp_path, [""])
    assert result["status"] == "error"
    assert "no extractable text" in result["error"]
