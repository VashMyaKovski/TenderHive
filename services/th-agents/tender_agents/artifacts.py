import errno
import json
import re
from hashlib import file_digest
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5
from xml.sax.saxutils import escape

from docx import Document
from pydantic import JsonValue
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from xlsxwriter import Workbook

from contracts.agent_task import DocumentAgentTask
from contracts.models import Artifact, Estimate, PreparationPlan, Recommendation, SolverResult

type DocumentKind = Literal["DOCX", "PDF", "XLSX"]
type FileExtension = Literal["docx", "pdf", "xlsx"]

MIME_TYPES: dict[FileExtension, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_VERSION = "proposal-v1"
_KINDS: tuple[tuple[DocumentKind, FileExtension], ...] = (
    ("DOCX", "docx"),
    ("PDF", "pdf"),
    ("XLSX", "xlsx"),
)
_XML_CONTROL = re.compile(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


class ArtifactConflict(Exception):
    pass


class ArtifactStore:
    def __init__(self, root: Path, pdf_font_path: Path) -> None:
        self._root = root
        self._font_path = pdf_font_path
        self._font_name = f"TenderHive-{pdf_font_path.resolve()}"

    def prepare(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        pdfmetrics.registerFont(TTFont(self._font_name, str(self._font_path)))

    def file_path(self, job_id: UUID, task_id: UUID, extension: FileExtension) -> Path:
        return self._directory(job_id, task_id) / f"proposal.{extension}"

    def _directory(self, job_id: UUID, task_id: UUID) -> Path:
        return self._root / str(job_id) / str(task_id)

    def render(self, task: DocumentAgentTask, fingerprint: str) -> list[Artifact]:
        output = self._directory(task.job_id, task.task_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.exists():
            self._render_batch(output, task, fingerprint)
        if (output / ".fingerprint").read_text(encoding="utf-8") != fingerprint:
            raise ArtifactConflict
        artifacts = []
        for kind, extension in _KINDS:
            with self.file_path(task.job_id, task.task_id, extension).open("rb") as content:
                checksum = file_digest(content, "sha256").hexdigest()
            artifacts.append(
                Artifact(
                    artifact_id=uuid5(NAMESPACE_URL, f"tenderhive:artifact:{task.task_id}:{kind}"),
                    kind=kind,
                    file_path=f"/api/v1/artifacts/{task.job_id}/{task.task_id}/{extension}",
                    version=_VERSION,
                    kb_version=task.kb_version,
                    checksum=checksum,
                )
            )
        return artifacts

    def _render_batch(self, output: Path, task: DocumentAgentTask, fingerprint: str) -> None:
        result = task.input.solver_result
        with TemporaryDirectory(dir=output.parent) as temporary:
            staged = Path(temporary)
            _docx(staged / "proposal.docx", result)
            _pdf(staged / "proposal.pdf", result, self._font_name)
            _xlsx(staged / "proposal.xlsx", result)
            (staged / ".fingerprint").write_text(fingerprint, encoding="utf-8")
            try:
                staged.rename(output)
            except OSError as error:
                # A retry may have finished the same file set after the original lease expired.
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not output.is_dir():
                    raise


def _text(value: object) -> str:
    return _XML_CONTROL.sub("", str(value))


def _json_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return _text(value)
    return _text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _docx(path: Path, result: SolverResult) -> None:
    document = Document()
    document.add_heading("Tender proposal", level=0)
    document.add_paragraph(f"Job {result.job_id} · Knowledge base {_text(result.kb_version)}")
    if isinstance(result.recommendation, Recommendation):
        document.add_heading("Recommendation", level=1)
        document.add_paragraph(_text(result.recommendation.value))
        for condition in result.recommendation.conditions:
            document.add_paragraph(_json_text(condition), style="List Bullet")
    document.add_heading("Requirements checklist", level=1)
    table = document.add_table(rows=1, cols=3)
    table.style = "Light Shading Accent 1"
    for cell, value in zip(table.rows[0].cells, ("Requirement", "Status", "Source"), strict=True):
        cell.text = value
    compliance = {item.requirement_id: item for item in result.compliance}
    for requirement in result.requirements:
        record = compliance.get(requirement.requirement_id)
        cells = table.add_row().cells
        cells[0].text = _text(requirement.text)
        cells[1].text = record.status if record else "Pending"
        if record:
            cells[1].add_paragraph(_text(record.reason))
        cells[2].text = f"{requirement.source.file_id} · page {requirement.source.page}"
    if isinstance(result.plan, PreparationPlan):
        document.add_heading("Preparation plan", level=1)
        for title, items in (
            ("Steps", result.plan.steps),
            ("Deadlines", result.plan.deadlines),
            ("Owners", result.plan.owners),
        ):
            if items:
                document.add_heading(title, level=2)
                for item in items:
                    document.add_paragraph(_json_text(item), style="List Bullet")
    document.save(str(path))


def _pdf(path: Path, result: SolverResult, font_name: str) -> None:
    title = ParagraphStyle(
        "Title", fontName=font_name, fontSize=20, leading=26, textColor=colors.HexColor("#153149")
    )
    body = ParagraphStyle("Body", fontName=font_name, fontSize=10, leading=15, alignment=TA_LEFT)
    report = SimpleDocTemplate(str(path), pagesize=A4, title="Tender risk report")
    content = [
        Paragraph("Tender risk report", title),
        Spacer(1, 8),
        Paragraph(f"Job {result.job_id} · Knowledge base {escape(_text(result.kb_version))}", body),
        Spacer(1, 16),
    ]
    if not result.risks:
        content.append(Paragraph("No risks recorded in the approved result.", body))
    for risk in result.risks:
        lines = [
            f"{risk.category} ({risk.level})",
            risk.basis,
            f"Mitigation: {_json_text(risk.mitigation)}",
            f"Source: {_json_text(risk.source_ref)}",
        ]
        content.extend(Paragraph(escape(_text(line)), body) for line in lines)
        content.append(Spacer(1, 10))
    if result.explanation.rule_trace:
        content.append(Paragraph("Rule trace", title))
        content.extend(
            Paragraph(escape(_json_text(rule)), body) for rule in result.explanation.rule_trace
        )
    report.build(content)


def _xlsx(path: Path, result: SolverResult) -> None:
    with Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        sheet = workbook.add_worksheet("Estimate")
        header = workbook.add_format({"bold": True, "bg_color": "#D8E5ED"})
        amount = workbook.add_format({"num_format": "#,##0.00##"})
        sheet.set_column(0, 0, 32)
        sheet.set_column(1, 3, 20)
        sheet.write_row(0, 0, ["Position", "Quantity", "Unit cost", "Cost"], header)
        sheet.freeze_panes(1, 0)
        if isinstance(result.estimate, Estimate):
            for row, position in enumerate(result.estimate.positions, 1):
                if isinstance(position, dict):
                    sheet.write_string(row, 0, _json_text(position.get("position_id", position)))
                    for column, field in enumerate(("quantity", "unit_cost", "cost"), 1):
                        value = position.get(field)
                        if isinstance(value, int | float | str) and not isinstance(value, bool):
                            try:
                                number = float(value)
                            except ValueError, OverflowError:
                                number = float("nan")
                            if isfinite(number):
                                sheet.write_number(row, column, number, amount)
                            else:
                                sheet.write_string(row, column, _text(value))
                else:
                    sheet.write_string(row, 0, _json_text(position))
            summary = (
                ("Total cost", result.estimate.total_cost),
                ("Offer price", result.estimate.offer_price),
                ("Margin (%)", result.estimate.margin_percent),
                ("Margin threshold (%)", result.estimate.threshold),
            )
            summary_row = len(result.estimate.positions) + 2
            for row, (label, value) in enumerate(summary, summary_row):
                sheet.write_string(row, 0, label, header)
                number = float(value)
                if isfinite(number):
                    sheet.write_number(row, 1, number, amount)
                else:
                    sheet.write_string(row, 1, _text(value))
            sheet.write_string(summary_row + len(summary), 0, "Threshold source", header)
            sheet.write_string(
                summary_row + len(summary), 1, _json_text(result.estimate.threshold_source)
            )
        else:
            sheet.write_string(1, 0, "No estimate recorded in the approved result.")
