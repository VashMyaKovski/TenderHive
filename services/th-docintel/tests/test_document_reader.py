from io import BytesIO
from uuid import uuid4

import pymupdf
import pytest
from docx import Document as WordDocument
from PIL import Image

from tender_docintel.document_reader import DocumentReader
from tender_docintel.errors import DocIntelError


def reader(*, text_layer_min_chars: int = 20, chunk_size: int = 12000) -> DocumentReader:
    return DocumentReader(
        max_pages=10,
        max_blocks=100,
        max_image_pixels=2_000_000,
        max_uncompressed_bytes=4 * 1024 * 1024,
        ocr_lang="rus+eng",
        ocr_dpi=100,
        chunk_size=chunk_size,
        text_layer_min_chars=text_layer_min_chars,
    )


def pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def docx_bytes() -> bytes:
    document = WordDocument()
    document.add_paragraph("Penalty is 0.15 percent per day")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Quantity"
    table.cell(0, 1).text = "Price"
    table.cell(1, 0).text = "2"
    table.cell(1, 1).text = "10.00"
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def docx_bytes_with_long_paragraph() -> bytes:
    document = WordDocument()
    document.add_paragraph("Tender condition " * 40)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_extracts_pdf_text_and_stable_source_coordinates() -> None:
    file_id = uuid4()
    first = reader().parse(pdf_bytes("Penalty is 0.15 percent per day"), file_id)
    second = reader().parse(pdf_bytes("Penalty is 0.15 percent per day"), file_id)

    assert len(first.blocks) == 1
    assert first.blocks[0].text == "Penalty is 0.15 percent per day"
    assert first.blocks[0].ocr_confidence == 1.0
    assert first.blocks[0].bbox == second.blocks[0].bbox
    assert first.blocks[0].block_id == second.blocks[0].block_id


def test_extracts_docx_paragraphs_and_table_rows() -> None:
    parsed = reader().parse(docx_bytes(), uuid4())

    assert parsed.page_count == 1
    assert parsed.blocks[0].text == "Penalty is 0.15 percent per day"
    assert parsed.blocks[1].kind == "table_row"
    assert parsed.tables[0].rows[0].cells == ["Quantity", "Price"]
    assert parsed.blocks[0].bbox == (0.0, 0.0, 0.0, 0.0)


def test_splits_long_paragraphs_into_configured_blocks() -> None:
    parsed = reader(chunk_size=100).parse(docx_bytes_with_long_paragraph(), uuid4())

    assert len(parsed.blocks) > 1
    assert all(len(block.text) <= 100 for block in parsed.blocks)


def test_rejects_unsupported_and_corrupted_files() -> None:
    with pytest.raises(DocIntelError) as unsupported:
        reader().parse(b"not a supported file", uuid4())
    assert unsupported.value.code == "UNSUPPORTED_FORMAT"

    with pytest.raises(DocIntelError) as corrupted:
        reader().parse(b"%PDF-invalid", uuid4())
    assert corrupted.value.code == "CORRUPTED_FILE"


def test_ocr_keeps_confidence_and_page_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_image_to_data(*_args: object, **_kwargs: object) -> dict[str, list[object]]:
        return {
            "text": ["Срок", "поставки"],
            "conf": ["62", "62"],
            "left": [10, 70],
            "top": [20, 20],
            "width": [50, 80],
            "height": [15, 15],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 1],
        }

    monkeypatch.setattr("pytesseract.image_to_data", fake_image_to_data)
    image = Image.new("RGB", (200, 100), "white")
    output = BytesIO()
    image.save(output, format="PNG")

    parsed = reader().parse(output.getvalue(), uuid4())

    assert parsed.blocks[0].text == "Срок поставки"
    assert parsed.blocks[0].ocr_confidence == pytest.approx(0.62)
    assert parsed.blocks[0].bbox == (10.0, 20.0, 150.0, 35.0)
