from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import UUID

from contracts.docintel_v1 import ExtractedTable, ExtractionWarning, SourceReference, TableRow
from tender_docintel.document_reader import ParsedTableRow

if TYPE_CHECKING:
    from tender_docintel.document_reader import ParsedTable

_NUMBER = re.compile(r"[-+]?[0-9][0-9 \u00a0]*(?:[,.][0-9]+)?")
_QUANTITY = ("количество", "кол-во", "quantity", "qty")
_PRICE = ("цена", "стоимость единицы", "unit price", "price")
_TOTAL = ("сумма", "итого", "стоимость", "total", "amount")


@dataclass(frozen=True, slots=True)
class TableAssessment:
    table: ExtractedTable
    warnings: list[ExtractionWarning]


def assess_tables(tables: list[ParsedTable], file_id: UUID, tolerance: float) -> TableAssessment:
    output_rows: list[TableRow] = []
    warnings: list[ExtractionWarning] = []
    checked = False
    mismatch = False
    global_row = 0

    for table in tables:
        for row in table.rows:
            output_rows.append(
                TableRow(
                    row_index=global_row,
                    cells=row.cells,
                    source=SourceReference(
                        file_id=file_id,
                        page=row.block.page,
                        block_id=row.block.block_id,
                        bbox=row.block.bbox,
                    ),
                )
            )
            global_row += 1
        result, row_mismatches = _check_table(table, tolerance)
        if result is not None:
            checked = True
            mismatch = mismatch or not result
        warnings.extend(
            ExtractionWarning(
                code="SUM_MISMATCH",
                message="Table arithmetic does not match the extracted row values.",
                source=SourceReference(
                    file_id=file_id,
                    page=row.block.page,
                    block_id=row.block.block_id,
                    bbox=row.block.bbox,
                ),
            )
            for row in row_mismatches
        )

    return TableAssessment(
        table=ExtractedTable(
            rows=output_rows,
            sum_check=False if mismatch else True if checked else None,
        ),
        warnings=warnings,
    )


def _check_table(table: ParsedTable, tolerance: float) -> tuple[bool | None, list[ParsedTableRow]]:
    if len(table.rows) < 2:
        return None, []
    header_index = _header_index(table)
    if header_index is None:
        return None, []

    headers = [_normalized(cell) for cell in table.rows[header_index].cells]
    quantity_index = _column(headers, _QUANTITY)
    price_index = _column(headers, _PRICE)
    total_index = _column(headers, _TOTAL, exclude={quantity_index, price_index})
    if quantity_index is None or price_index is None or total_index is None:
        return None, []

    tolerance_value = Decimal(str(tolerance))
    computed: list[tuple[Decimal, ParsedTableRow]] = []
    total_rows: list[tuple[Decimal, ParsedTableRow]] = []
    mismatches: list[ParsedTableRow] = []
    for row in table.rows[header_index + 1 :]:
        if not row.cells:
            continue
        joined = " ".join(row.cells)
        if _is_summary(joined):
            amount = _decimal_at(row.cells, total_index)
            if amount is not None:
                total_rows.append((amount, row))
            continue
        quantity = _decimal_at(row.cells, quantity_index)
        price = _decimal_at(row.cells, price_index)
        actual = _decimal_at(row.cells, total_index)
        if quantity is None and price is None and actual is None:
            continue
        if quantity is None or price is None or actual is None:
            mismatches.append(row)
            continue
        expected = quantity * price
        computed.append((expected, row))
        if abs(expected - actual) > tolerance_value:
            mismatches.append(row)

    if not computed:
        return None, []
    summary = sum((value for value, _ in computed), Decimal(0))
    if total_rows and abs(summary - total_rows[-1][0]) > tolerance_value:
        mismatches.append(total_rows[-1][1])
    return not mismatches, mismatches


def _header_index(table: ParsedTable) -> int | None:
    for index, row in enumerate(table.rows[:4]):
        cells = [_normalized(cell) for cell in row.cells]
        if _column(cells, _QUANTITY) is not None and _column(cells, _PRICE) is not None:
            return index
    return None


def _column(
    headers: list[str], keywords: tuple[str, ...], exclude: set[int | None] | None = None
) -> int | None:
    excluded = exclude or set()
    for index, header in enumerate(headers):
        if index not in excluded and any(keyword in header for keyword in keywords):
            return index
    return None


def _decimal_at(cells: list[str], index: int) -> Decimal | None:
    if index >= len(cells):
        return None
    match = _NUMBER.search(cells[index])
    if match is None:
        return None
    value = match.group().replace("\u00a0", "").replace(" ", "")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _is_summary(text: str) -> bool:
    normalized = _normalized(text)
    return "итого" in normalized or "всего" in normalized or "total" in normalized


def _normalized(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())
