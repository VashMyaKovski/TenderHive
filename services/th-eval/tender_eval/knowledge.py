from hmac import compare_digest
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import Field, HttpUrl, JsonValue, model_validator

from tender_eval.cases import Contract, Version
from tender_eval.config import ApiSettings


class Expected(Contract):
    status: Literal["READY", "INCOMPLETE", "NO_SOLUTION"]
    stop_reasons: list[str]
    compliance: list[Literal["MET", "NOT_MET", "NEEDS_REVIEW"]] | None = None


class Case(Contract):
    case_id: Version
    input: dict[str, JsonValue]
    expected: Expected


class CheckRequest(Contract):
    candidate_version: Version
    baseline_version: Version | None
    test_set_version: Version
    test_cases: Annotated[list[Case], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def validate_case_ids(self) -> Self:
        if len({case.case_id for case in self.test_cases}) != len(self.test_cases):
            raise ValueError("duplicate test case")
        return self


class ComplianceObservation(Contract):
    model_config = {"extra": "ignore"}
    status: Literal["MET", "NOT_MET", "NEEDS_REVIEW"]


class Observation(Contract):
    model_config = {"extra": "ignore"}
    status: Literal["READY", "INCOMPLETE", "NO_SOLUTION"]
    kb_version: Version
    stop_reasons: list[str]
    compliance: list[ComplianceObservation]


class ReportEntry(Contract):
    code: str
    message: str


class CheckResult(Contract):
    passed: bool
    report: list[ReportEntry]


class ExpectedWorkflow(Contract):
    job_id: UUID
    kb_version: Version
    phase: Literal["accepted", "awaiting_review", "awaiting_artifacts", "done", "failed"]


def _matches(observation: Observation, expected: Expected) -> bool:
    return (
        observation.status == expected.status
        and sorted(observation.stop_reasons) == sorted(expected.stop_reasons)
        and (
            expected.compliance is None
            or [row.status for row in observation.compliance] == expected.compliance
        )
    )


async def _observe(client: httpx.AsyncClient, case: Case, version: str, token: str) -> Observation:
    body = dict(case.input)
    body.setdefault("job_id", str(uuid4()))
    for key, value in {
        "trace_id": str(uuid4()),
        "kb_version": version,
    }.items():
        body[key] = value
    response = await client.post(
        "/api/v1/solve",
        json=body,
        headers={"X-Trace-Id": str(body["trace_id"]), "X-KB-Eval-Token": token},
    )
    response.raise_for_status()
    observation = Observation.model_validate_json(response.content)
    if observation.kb_version != version or response.headers.get("X-Trace-Id") != body["trace_id"]:
        raise ValueError("uncorrelated result")
    return observation


async def check(
    body: CheckRequest,
    solver: HttpUrl,
    token: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> CheckResult:
    report: list[ReportEntry] = []
    async with httpx.AsyncClient(
        base_url=str(solver), timeout=timeout_seconds, trust_env=False, transport=transport
    ) as client:
        for case in body.test_cases:
            try:
                if body.baseline_version is not None:
                    baseline = await _observe(client, case, body.baseline_version, token)
                else:
                    baseline = None
                candidate = await _observe(client, case, body.candidate_version, token)
            except httpx.HTTPError, ValueError:
                raise HTTPException(
                    status_code=503, detail="Solver evaluation unavailable"
                ) from None
            if not _matches(candidate, case.expected):
                regressed = baseline is not None and _matches(baseline, case.expected)
                code = "REGRESSION_DETECTED" if regressed else "EXPECTED_RESULT_MISMATCH"
                report.append(ReportEntry(code=code, message=f"Case {case.case_id} failed"))
    return CheckResult(passed=not report, report=report)


def router_for(settings: ApiSettings) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/kb/check")
    async def check_version(
        body: CheckRequest, token: Annotated[str | None, Header(alias="X-KB-Eval-Token")] = None
    ) -> CheckResult:
        if (
            settings.kb_eval_token is None
            or token is None
            or not compare_digest(
                token.encode(), settings.kb_eval_token.get_secret_value().encode()
            )
        ):
            raise HTTPException(status_code=403, detail="Evaluation key required")
        if settings.solver_url is None:
            raise HTTPException(status_code=503, detail="Solver evaluation unavailable")
        return await check(body, settings.solver_url, token, settings.timeout_seconds, None)

    @router.post("/api/v1/workflow/check")
    async def check_workflow(
        body: ExpectedWorkflow, token: Annotated[str | None, Header(alias="X-KB-Eval-Token")] = None
    ) -> CheckResult:
        if (
            settings.kb_eval_token is None
            or token is None
            or not compare_digest(
                token.encode(), settings.kb_eval_token.get_secret_value().encode()
            )
        ):
            raise HTTPException(status_code=403, detail="Evaluation key required")
        if settings.orchestrator_url is None or settings.orchestrator_token is None:
            raise HTTPException(status_code=503, detail="Workflow evaluation unavailable")
        trace = str(uuid4())
        try:
            async with httpx.AsyncClient(
                base_url=str(settings.orchestrator_url),
                timeout=settings.timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{body.job_id}",
                    headers={
                        "Authorization": "Bearer " + settings.orchestrator_token.get_secret_value(),
                        "X-Trace-Id": trace,
                    },
                )
                response.raise_for_status()
                view = response.json()
            passed = (
                response.headers.get("X-Trace-Id") == trace
                and view["job"]["job_id"] == str(body.job_id)
                and view["job"]["kb_version"] == body.kb_version
                and view["phase"] == body.phase
            )
            if body.phase == "done":
                files = view["artifacts"]
                passed = (
                    passed
                    and view["job"]["status"] == "DONE"
                    and len(files) == 3
                    and {item["kind"] for item in files} == {"DOCX", "PDF", "XLSX"}
                    and any(
                        card["reason_code"] == "FINAL_APPROVAL"
                        and (card.get("decision") or {}).get("decision") == "APPROVED"
                        for card in view["cards"]
                    )
                )
            elif body.phase == "awaiting_review":
                passed = (
                    passed
                    and not view["artifacts"]
                    and any(card["state"] == "open" for card in view["cards"])
                )
        except httpx.HTTPError, KeyError, TypeError, ValueError:
            raise HTTPException(status_code=503, detail="Workflow evaluation unavailable") from None
        return CheckResult(
            passed=passed,
            report=[]
            if passed
            else [
                ReportEntry(
                    code="WORKFLOW_MISMATCH", message="Job snapshot did not match expectations."
                )
            ],
        )

    return router
