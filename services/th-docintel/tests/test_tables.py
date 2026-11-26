from uuid import uuid4

from tender_docintel.document_reader import ParsedTable, ParsedTableRow
from tender_docintel.models import DocumentBlock
from tender_docintel.tables import assess_tables


def table_row(index: int, cells: list[str]) -> ParsedTableRow:
    block = DocumentBlock(
        block_id=uuid4(),
        page=1,
        text=" | ".join(cells),
        bbox=(10.0, float(index * 20), 400.0, float(index * 20 + 18)),
        kind="table_row",
        ocr_confidence=1.0,
    )
    return ParsedTableRow(cells=cells, block=block)


def test_checks_line_and_document_totals() -> None:
    table = ParsedTable(
        rows=[
            table_row(0, ["Позиция", "Количество", "Цена, BYN", "Сумма, BYN"]),
            table_row(1, ["Кабель", "2", "10,00", "20,00"]),
            table_row(2, ["Итого", "", "", "20,00"]),
        ]
    )

    result = assess_tables([table], uuid4(), 0.01)

    assert result.table.sum_check is True
    assert result.warnings == []
    assert len(result.table.rows) == 3


def test_reports_sum_mismatch_with_source() -> None:
    table = ParsedTable(
        rows=[
            table_row(0, ["Позиция", "Количество", "Цена, BYN", "Сумма, BYN"]),
            table_row(1, ["Кабель", "2", "10,00", "21,00"]),
        ]
    )

    result = assess_tables([table], uuid4(), 0.01)

    assert result.table.sum_check is False
    assert [warning.code for warning in result.warnings] == ["SUM_MISMATCH"]
    assert result.warnings[0].source == result.table.rows[1].source


def test_leaves_sum_check_unknown_when_column_roles_are_not_known() -> None:
    table = ParsedTable(rows=[table_row(0, ["Название", "Текст"]), table_row(1, ["A", "B"])])

    result = assess_tables([table], uuid4(), 0.01)

    assert result.table.sum_check is None
