from __future__ import annotations

import zipfile
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from statistics import median
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid5

import pymupdf
import pytesseract
from docx import Document as WordDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table as WordTable
from docx.text.paragraph import Paragraph
from PIL import Image, UnidentifiedImageError
from pytesseract import TesseractNotFoundError

from tender_docintel.errors import DocIntelError
from tender_docintel.models import DocumentBlock

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ParsedTableRow:
    cells: list[str]
    block: DocumentBlock


@dataclass(frozen=True, slots=True)
class ParsedTable:
    rows: list[ParsedTableRow]


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    blocks: list[DocumentBlock]
    tables: list[ParsedTable]
    page_count: int


@dataclass(frozen=True, slots=True)
class OCRWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True, slots=True)
class OCRLine:
    block: DocumentBlock
    words: list[OCRWord]


class DocumentReader:
    def __init__(
        self,
        *,
        max_pages: int,
        max_blocks: int,
        max_image_pixels: int,
        max_uncompressed_bytes: int,
        ocr_lang: str,
        ocr_dpi: int,
        chunk_size: int,
        text_layer_min_chars: int,
    ) -> None:
        self._max_pages = max_pages
        self._max_blocks = max_blocks
        self._max_image_pixels = max_image_pixels
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._ocr_lang = ocr_lang
        self._ocr_dpi = ocr_dpi
        self._chunk_size = chunk_size
        self._text_layer_min_chars = text_layer_min_chars

    def parse(self, content: bytes, file_id: UUID) -> ParsedDocument:
        if not content:
            raise DocIntelError("CORRUPTED_FILE")
        if content.startswith(b"%PDF-"):
            return self._parse_pdf(content, file_id)
        if self._is_docx(content):
            return self._parse_docx(content, file_id)
        return self._parse_image(content, file_id)

    def _is_docx(self, content: bytes) -> bool:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                if (
                    sum(item.file_size for item in archive.infolist())
                    > self._max_uncompressed_bytes
                ):
                    raise DocIntelError("FILE_TOO_LARGE")
                return "word/document.xml" in archive.namelist()
        except DocIntelError:
            raise
        except OSError, zipfile.BadZipFile:
            return False

    def _parse_pdf(self, content: bytes, file_id: UUID) -> ParsedDocument:
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
        except pymupdf.FileDataError, ValueError, RuntimeError:
            raise DocIntelError("CORRUPTED_FILE") from None
        with document:
            if document.is_encrypted or document.page_count == 0:
                raise DocIntelError("CORRUPTED_FILE")
            if document.page_count > self._max_pages:
                raise DocIntelError("TOO_MANY_PAGES")
            blocks: list[DocumentBlock] = []
            tables: list[ParsedTable] = []
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                page_blocks, page_tables = self._parse_pdf_page(page, file_id, page_index + 1)
                blocks.extend(page_blocks)
                tables.extend(page_tables)
                self._check_block_limit(blocks)
            blocks.sort(
                key=lambda block: (block.page, block.bbox[1], block.bbox[0], str(block.block_id))
            )
            if not blocks:
                raise DocIntelError("CORRUPTED_FILE")
            return ParsedDocument(blocks=blocks, tables=tables, page_count=document.page_count)

    def _parse_pdf_page(
        self, page: pymupdf.Page, file_id: UUID, page_number: int
    ) -> tuple[list[DocumentBlock], list[ParsedTable]]:
        table_blocks, tables, table_boxes = self._extract_pdf_tables(page, file_id, page_number)
        page_text = cast(str, page.get_text("text"))
        if len(page_text.strip()) < self._text_layer_min_chars:
            try:
                rendered_width = page.rect.width * self._ocr_dpi / 72
                rendered_height = page.rect.height * self._ocr_dpi / 72
                if rendered_width * rendered_height > self._max_image_pixels:
                    raise DocIntelError("IMAGE_TOO_LARGE")
                pixmap = page.get_pixmap(dpi=self._ocr_dpi, alpha=False)
                image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
            except DocIntelError:
                raise
            except RuntimeError, ValueError, OSError:
                raise DocIntelError("CORRUPTED_FILE") from None
            lines = self._ocr_lines(image, file_id, page_number)
            ocr_blocks, ocr_tables = self._reconstruct_ocr_tables(lines, file_id, page_number)
            return ocr_blocks, ocr_tables

        text_blocks = self._extract_pdf_text(page, file_id, page_number, table_boxes)
        return table_blocks + text_blocks, tables

    def _extract_pdf_tables(
        self, page: pymupdf.Page, file_id: UUID, page_number: int
    ) -> tuple[list[DocumentBlock], list[ParsedTable], list[tuple[float, float, float, float]]]:
        blocks: list[DocumentBlock] = []
        tables: list[ParsedTable] = []
        boxes: list[tuple[float, float, float, float]] = []
        try:
            found = page.find_tables()
        except AttributeError, RuntimeError, ValueError:
            return blocks, tables, boxes
        if found is None:
            return blocks, tables, boxes

        for table_index, table in enumerate(found.tables):
            table_box = (
                float(table.bbox[0]),
                float(table.bbox[1]),
                float(table.bbox[2]),
                float(table.bbox[3]),
            )
            boxes.append(table_box)
            row_results: list[ParsedTableRow] = []
            try:
                extracted_rows = table.extract()
            except RuntimeError, ValueError:
                continue
            for row_index, extracted in enumerate(extracted_rows):
                cells = [str(cell or "").strip() for cell in extracted]
                row_text = " | ".join(cells).strip(" |")
                if not row_text:
                    continue
                row_box = self._pdf_table_row_box(table, row_index, table_box)
                row_blocks = [
                    self._make_block(
                        file_id,
                        page_number,
                        f"pdf-table:{table_index}:row:{row_index}:chunk:{chunk_index}",
                        chunk,
                        row_box,
                        "table_row",
                        1.0,
                    )
                    for chunk_index, chunk in enumerate(self._split_text(row_text))
                ]
                blocks.extend(row_blocks)
                row_results.append(ParsedTableRow(cells=cells, block=row_blocks[0]))
            if row_results:
                tables.append(ParsedTable(rows=row_results))
        return blocks, tables, boxes

    @staticmethod
    def _pdf_table_row_box(
        table: object,
        row_index: int,
        fallback: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        try:
            row = table.rows[row_index]  # type: ignore[attr-defined]
            cells = [cell for cell in row.cells if cell is not None]
            if not cells:
                return fallback
            return (
                min(float(cell[0]) for cell in cells),
                min(float(cell[1]) for cell in cells),
                max(float(cell[2]) for cell in cells),
                max(float(cell[3]) for cell in cells),
            )
        except AttributeError, IndexError, TypeError, ValueError:
            return fallback

    def _extract_pdf_text(
        self,
        page: pymupdf.Page,
        file_id: UUID,
        page_number: int,
        table_boxes: list[tuple[float, float, float, float]],
    ) -> list[DocumentBlock]:
        blocks: list[DocumentBlock] = []
        try:
            extracted = page.get_text("blocks")
        except RuntimeError, ValueError:
            raise DocIntelError("CORRUPTED_FILE") from None
        for index, raw in enumerate(extracted):
            if len(raw) < 7 or int(raw[6]) != 0:
                continue
            text = str(raw[4]).strip()
            if not text:
                continue
            bbox = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
            if any(self._overlaps(bbox, table_box) for table_box in table_boxes):
                continue
            for chunk_index, chunk in enumerate(self._split_text(text)):
                blocks.append(
                    self._make_block(
                        file_id,
                        page_number,
                        f"pdf-text:{index}:chunk:{chunk_index}",
                        chunk,
                        bbox,
                        "text",
                        1.0,
                    )
                )
        return blocks

    def _parse_docx(self, content: bytes, file_id: UUID) -> ParsedDocument:
        try:
            document = WordDocument(BytesIO(content))
        except KeyError, ValueError, zipfile.BadZipFile:
            raise DocIntelError("CORRUPTED_FILE") from None
        blocks: list[DocumentBlock] = []
        tables: list[ParsedTable] = []
        page_number = 1
        global_index = 0
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if text:
                    for chunk in self._split_text(text):
                        blocks.append(
                            self._make_block(
                                file_id,
                                page_number,
                                global_index,
                                chunk,
                                (0.0, 0.0, 0.0, 0.0),
                                "text",
                                1.0,
                            )
                        )
                        global_index += 1
                page_number += self._explicit_page_breaks(child)
            elif isinstance(child, CT_Tbl):
                table = WordTable(child, document)
                rows: list[ParsedTableRow] = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    row_text = " | ".join(cells).strip(" |")
                    if not row_text:
                        continue
                    row_blocks = [
                        self._make_block(
                            file_id,
                            page_number,
                            global_index + chunk_index,
                            chunk,
                            (0.0, 0.0, 0.0, 0.0),
                            "table_row",
                            1.0,
                        )
                        for chunk_index, chunk in enumerate(self._split_text(row_text))
                    ]
                    blocks.extend(row_blocks)
                    rows.append(ParsedTableRow(cells=cells, block=row_blocks[0]))
                    global_index += len(row_blocks)
                if rows:
                    tables.append(ParsedTable(rows=rows))
        if page_number > self._max_pages:
            raise DocIntelError("TOO_MANY_PAGES")
        self._check_block_limit(blocks)
        if not blocks:
            raise DocIntelError("CORRUPTED_FILE")
        return ParsedDocument(blocks=blocks, tables=tables, page_count=page_number)

    @staticmethod
    def _explicit_page_breaks(paragraph: CT_P) -> int:
        return sum(
            1
            for element in paragraph.iter()
            if element.tag.endswith("}br")
            and element.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type")
            == "page"
        )

    def _parse_image(self, content: bytes, file_id: UUID) -> ParsedDocument:
        try:
            image = Image.open(BytesIO(content))
            image_format = image.format
            image.verify()
            if image_format not in {"PNG", "JPEG", "TIFF", "BMP", "WEBP"}:
                raise DocIntelError("UNSUPPORTED_FORMAT")
            if image.width * image.height > self._max_image_pixels:
                raise DocIntelError("IMAGE_TOO_LARGE")
            image = Image.open(BytesIO(content)).convert("RGB")
        except DocIntelError:
            raise
        except Image.DecompressionBombError:
            raise DocIntelError("IMAGE_TOO_LARGE") from None
        except UnidentifiedImageError, OSError, ValueError:
            raise DocIntelError("UNSUPPORTED_FORMAT") from None
        lines = self._ocr_lines(image, file_id, 1)
        blocks, tables = self._reconstruct_ocr_tables(lines, file_id, 1)
        self._check_block_limit(blocks)
        if not blocks:
            raise DocIntelError("CORRUPTED_FILE")
        return ParsedDocument(blocks=blocks, tables=tables, page_count=1)

    def _ocr_lines(
        self,
        image: Image.Image,
        file_id: UUID,
        page_number: int,
    ) -> list[OCRLine]:
        try:
            data = pytesseract.image_to_data(
                image, lang=self._ocr_lang, output_type=pytesseract.Output.DICT
            )
        except TesseractNotFoundError:
            raise DocIntelError("OCR_UNAVAILABLE") from None
        except pytesseract.TesseractError:
            raise DocIntelError("CORRUPTED_FILE") from None

        grouped: dict[tuple[int, int, int], list[OCRWord]] = defaultdict(list)
        for index, text in enumerate(data["text"]):
            value = str(text).strip()
            if not value:
                continue
            try:
                confidence = float(data["conf"][index]) / 100.0
                if confidence < 0:
                    continue
                word = OCRWord(
                    value,
                    int(data["left"][index]),
                    int(data["top"][index]),
                    int(data["width"][index]),
                    int(data["height"][index]),
                    min(confidence, 1.0),
                )
                key = (
                    int(data["block_num"][index]),
                    int(data["par_num"][index]),
                    int(data["line_num"][index]),
                )
                grouped[key].append(word)
            except KeyError, TypeError, ValueError, IndexError:
                continue

        lines: list[OCRLine] = []
        for line_index, words in enumerate(grouped.values()):
            words.sort(key=lambda word: word.left)
            text = " ".join(word.text for word in words)
            bbox = (
                float(min(word.left for word in words)),
                float(min(word.top for word in words)),
                float(max(word.left + word.width for word in words)),
                float(max(word.top + word.height for word in words)),
            )
            confidence = sum(word.confidence for word in words) / len(words)
            block = self._make_block(
                file_id, page_number, line_index, text, bbox, "text", confidence
            )
            lines.append(OCRLine(block=block, words=words))
        lines.sort(key=lambda line: (line.block.bbox[1], line.block.bbox[0]))
        return lines

    def _reconstruct_ocr_tables(
        self, lines: list[OCRLine], file_id: UUID, page_number: int
    ) -> tuple[list[DocumentBlock], list[ParsedTable]]:
        candidates: list[tuple[OCRLine, list[str]]] = []
        for line in lines:
            heights = [word.height for word in line.words if word.height > 0]
            typical_height = median(heights) if heights else 0
            gap_limit = max(12.0, typical_height * 1.8)
            cell_words: list[list[OCRWord]] = []
            for word in line.words:
                if (
                    not cell_words
                    or word.left - (cell_words[-1][-1].left + cell_words[-1][-1].width) > gap_limit
                ):
                    cell_words.append([word])
                else:
                    cell_words[-1].append(word)
            if len(cell_words) > 1:
                candidates.append(
                    (line, [" ".join(word.text for word in cell) for cell in cell_words])
                )

        if len(candidates) < 2:
            return [line.block for line in lines], []
        groups: list[list[tuple[OCRLine, list[str]]]] = []
        for candidate in candidates:
            if (
                not groups
                or candidate[0].block.bbox[1] - groups[-1][-1][0].block.bbox[3]
                > max(30.0, candidate[0].block.bbox[3] - candidate[0].block.bbox[1])
                or abs(len(candidate[1]) - len(groups[-1][-1][1])) > 1
            ):
                groups.append([candidate])
            else:
                groups[-1].append(candidate)
        table_groups = [group for group in groups if len(group) >= 2]
        if not table_groups:
            return [line.block for line in lines], []

        table_lines = {id(line) for group in table_groups for line, _ in group}
        blocks = [line.block for line in lines if id(line) not in table_lines]
        tables: list[ParsedTable] = []
        for table_index, group in enumerate(table_groups):
            rows: list[ParsedTableRow] = []
            for row_index, (line, row_cells) in enumerate(group):
                block = self._make_block(
                    file_id,
                    page_number,
                    table_index * self._max_blocks + row_index,
                    " | ".join(row_cells),
                    line.block.bbox,
                    "table_row",
                    line.block.ocr_confidence,
                )
                blocks.append(block)
                rows.append(ParsedTableRow(cells=row_cells, block=block))
            tables.append(ParsedTable(rows=rows))
        return blocks, tables

    @staticmethod
    def _overlaps(
        first: tuple[float, float, float, float], second: tuple[float, float, float, float]
    ) -> bool:
        left = max(first[0], second[0])
        top = max(first[1], second[1])
        right = min(first[2], second[2])
        bottom = min(first[3], second[3])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
        return area == 0.0 or intersection / area >= 0.5

    def _make_block(
        self,
        file_id: UUID,
        page: int,
        index: int | str,
        text: str,
        bbox: tuple[float, float, float, float],
        kind: Literal["text", "table_cell", "table_row"],
        confidence: float,
    ) -> DocumentBlock:
        if len(text) > self._chunk_size:
            raise DocIntelError("TOO_MANY_BLOCKS")
        block_id = uuid5(file_id, f"{page}:{index}:{kind}:{text}")
        return DocumentBlock(
            block_id=block_id,
            page=page,
            text=text,
            bbox=bbox,
            kind=kind,
            ocr_confidence=max(0.0, min(confidence, 1.0)),
        )

    def _split_text(self, text: str) -> list[str]:
        chunks: list[str] = []
        offset = 0
        while offset < len(text):
            end = min(offset + self._chunk_size, len(text))
            if end < len(text):
                boundary = text.rfind(" ", offset, end)
                if boundary > offset:
                    end = boundary
            chunk = text[offset:end].strip()
            if chunk:
                chunks.append(chunk)
            offset = end
            while offset < len(text) and text[offset].isspace():
                offset += 1
        return chunks

    def _check_block_limit(self, blocks: Sequence[DocumentBlock]) -> None:
        if len(blocks) > self._max_blocks:
            raise DocIntelError("TOO_MANY_BLOCKS")
