import re
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

import httpx
from pydantic import HttpUrl, ValidationError

from contracts.docintel_v1 import ExtractionWarning, ExtractResponse, NumberValue, TableRow
from contracts.models import Contract, Requirement
from tender_orchestrator.workbench_inputs import ExtractionConfirmation, Upload


class ExtractionReview(Contract):
    status: Literal["OK", "INCOMPLETE", "REJECTED", "FAILED"]
    requirements: list[Requirement]
    warnings: list[str]
    numbers: list[NumberValue]
    table_rows: list[TableRow]
    issues: list[ExtractionWarning]


def _failure(status: Literal["REJECTED", "FAILED"], message: str) -> ExtractionReview:
    return ExtractionReview(
        status=status, requirements=[], warnings=[message], numbers=[], table_rows=[], issues=[]
    )


async def extract(
    http: httpx.AsyncClient, url: HttpUrl, job_id: UUID, trace_id: UUID, uploads: Sequence[Upload]
) -> ExtractionReview:
    results: list[ExtractResponse] = []
    for upload in uploads:
        document = upload.document
        try:
            response = await http.post(
                f"{str(url).rstrip('/')}/api/v1/extract",
                headers={"X-Trace-Id": str(trace_id)},
                data={"job_id": str(job_id), "file_id": str(document.file_id)},
                files={"file": (document.name, upload.data, document.mime)},
            )
        except httpx.HTTPError:
            return _failure("FAILED", f"Сервис разбора недоступен для файла {document.name}.")
        if not response.is_success:
            if response.status_code in {400, 413, 415, 422}:
                return _failure(
                    "REJECTED", f"Проверьте формат или содержимое файла {document.name}."
                )
            return _failure("FAILED", f"Не удалось обработать файл {document.name}.")
        try:
            result = ExtractResponse.model_validate_json(response.content)
            sources = [
                item.source for item in [*result.requirements, *result.numbers, *result.table.rows]
            ]
            sources.extend(issue.source for issue in result.warnings if issue.source is not None)
            if (
                result.job_id != job_id
                or result.trace_id != trace_id
                or any(source.file_id != document.file_id for source in sources)
            ):
                return _failure("FAILED", "Сервис вернул результаты для другого документа.")
        except ValidationError:
            return _failure("FAILED", "Ответ сервиса разбора имеет неизвестный формат.")
        if result.status == "REJECTED":
            return _failure("REJECTED", f"Сервис не смог разобрать файл {document.name}.")
        results.append(result)

    requirements = [
        Requirement.model_validate(item.model_dump())
        for result in results
        for item in result.requirements
    ]
    if len(requirements) > 200 or len({item.requirement_id for item in requirements}) != len(
        requirements
    ):
        return _failure("REJECTED", "Разбейте материалы на меньшие наборы без дубликатов.")

    issues = [issue for result in results for issue in result.warnings]
    warnings = [issue.message for issue in issues]
    warnings.extend(result.status_reason for result in results if result.status_reason)
    comparable: dict[tuple[str, str | None, str], list[Requirement]] = defaultdict(list)
    for item in requirements:
        if item.value is not None:
            normalized = " ".join(re.sub(r"\d+(?:[.,]\d+)?", "#", item.text.casefold()).split())
            comparable[(item.kind, item.unit, normalized)].append(item)
    conflicts = {
        item.requirement_id
        for group in comparable.values()
        if len({item.source.file_id for item in group}) > 1
        and len({str(item.value) for item in group}) > 1
        for item in group
    }
    if conflicts:
        warnings.append("В разных файлах указаны разные значения для похожих требований.")
        requirements = [
            item.model_copy(update={"needs_review": True})
            if item.requirement_id in conflicts
            else item
            for item in requirements
        ]
    if not requirements:
        warnings.append("Требования не извлечены. Проверьте документы перед продолжением.")
    return ExtractionReview(
        status="INCOMPLETE"
        if conflicts or not requirements or any(item.status == "INCOMPLETE" for item in results)
        else "OK",
        requirements=requirements,
        warnings=list(dict.fromkeys(warnings)),
        numbers=[number for result in results for number in result.numbers],
        table_rows=[row for result in results for row in result.table.rows],
        issues=issues,
    )


def confirm(review: ExtractionReview, confirmation: ExtractionConfirmation) -> ExtractionReview:
    edits = {item.requirement_id: item for item in confirmation.requirements}
    requirements = {item.requirement_id: item for item in review.requirements}
    if edits.keys() != requirements.keys() or len(edits) != len(confirmation.requirements):
        raise ValueError("Список требований изменился. Обновите страницу.")
    confirmed: list[Requirement] = []
    for identifier, requirement in requirements.items():
        edit = edits[identifier]
        value = edit.value
        if (
            isinstance(value, str)
            and requirement.unit in {"business_days", "calendar_days"}
            and value.isascii()
            and value.strip().isdigit()
        ):
            value = int(value)
        if (
            requirement.mandatory
            and requirement.needs_review
            and value is None
            and edit.text == requirement.text
        ):
            raise ValueError("Уточните текст или значение обязательного требования.")
        confirmed.append(
            requirement.model_copy(
                update={"text": edit.text, "value": value, "needs_review": False}
            )
        )
    return review.model_copy(update={"requirements": confirmed})


def confirmed_daily_penalty(review: ExtractionReview | None, context: str) -> Decimal | None:
    if review is None:
        return None
    matches: set[Decimal] = set()
    for item in review.requirements:
        if item.kind != "penalty" or item.unit != "fraction_per_day" or item.value is None:
            continue
        if item.text not in context and context not in item.text:
            continue
        if not isinstance(item.value, str | int | float) or isinstance(item.value, bool):
            continue
        try:
            rate = Decimal(str(item.value).replace(",", ".")) * 100
        except InvalidOperation:
            continue
        if rate.is_finite() and 0 <= rate <= 100:
            matches.add(rate)
    return next(iter(matches)) if len(matches) == 1 else None
