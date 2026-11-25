from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
from pydantic import Field, JsonValue, ValidationError

from tender_eval.cases import Contract, DatasetError, DatasetHeader, EvalCase
from tender_eval.gateway import INFERENCE_PATH, TRACE_HEADER


class Fixture(Contract):
    case_id: UUID
    status: Annotated[int, Field(ge=200, le=599)]
    response: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class FixtureSet:
    digest: str
    responses: dict[UUID, Fixture]

    def make_transport(self, prompt_version: str) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.method != "POST" or request.url.path != INFERENCE_PATH:
                return httpx.Response(404)
            request_data = _FixtureRequest.model_validate_json(request.content)
            if request.headers.get(TRACE_HEADER) != str(request_data.trace_id):
                return httpx.Response(422)
            fixture = self.responses[request_data.job_id]
            body = {"trace_id": str(request_data.trace_id), **fixture.response}
            if fixture.status == 200:
                body = {
                    "job_id": str(request_data.job_id),
                    "prompt_version": prompt_version,
                    "cached": False,
                    **body,
                }
            elif body.get("status") == "UNAVAILABLE":
                body["job_id"] = str(request_data.job_id)
            return httpx.Response(
                fixture.status, json=body, headers={TRACE_HEADER: str(request_data.trace_id)}
            )

        return httpx.MockTransport(respond)


class _FixtureRequest(Contract):
    job_id: UUID
    task: str
    context: list[JsonValue]
    prompt_version: str
    mask: bool
    trace_id: UUID


def load_fixtures(path: Path, dataset_version: str, cases: tuple[EvalCase, ...]) -> FixtureSet:
    try:
        content = path.read_bytes()
        lines = content.decode("utf-8").splitlines()
    except OSError, UnicodeError:
        raise DatasetError("cannot read UTF-8 HTTP fixtures") from None
    if not lines:
        raise DatasetError("HTTP fixtures require a header")
    try:
        header = DatasetHeader.model_validate_json(lines[0])
    except ValidationError:
        raise DatasetError("invalid HTTP fixture header") from None
    if header.dataset_version != dataset_version:
        raise DatasetError("HTTP fixtures reference a different dataset version")

    responses: dict[UUID, Fixture] = {}
    for number, line in enumerate(lines[1:], 2):
        try:
            fixture = Fixture.model_validate_json(line)
        except ValidationError:
            raise DatasetError(f"invalid HTTP fixture at line {number}") from None
        if fixture.case_id in responses:
            raise DatasetError(f"duplicate HTTP fixture at line {number}")
        responses[fixture.case_id] = fixture
    if set(responses) != {case.case_id for case in cases}:
        raise DatasetError("HTTP fixtures must match the dataset case IDs")

    return FixtureSet(digest=sha256(content).hexdigest(), responses=responses)
