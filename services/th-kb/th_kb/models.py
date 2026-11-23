import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Rule(Contract):
    rule_id: Identifier
    condition: Text
    consequence: Text
    priority: Annotated[int, Field(ge=0, le=1000)]
    scope: Identifier
    version: Annotated[int, Field(ge=1)]
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None


class Fact(Contract):
    fact_id: Identifier
    subject: Identifier
    key: Identifier
    value: JsonValue
    source: Identifier
    unit: str | None = None
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None


class TestCase(Contract):
    case_id: Identifier
    input_facts: dict[str, JsonValue]
    expected_rule_id: Identifier | None = None
    expected_consequence: Text | None = None


class VersionPayload(Contract):
    version: Identifier
    rules: list[Rule]
    facts: list[Fact]
    reference_data: dict[str, JsonValue]
    test_cases: list[TestCase]
    checksum: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    author: Identifier
    created_at: AwareDatetime


class VersionStatus(StrEnum):
    DRAFT = "DRAFT"
    CHECK = "CHECK"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    OLD = "OLD"


class VersionInfo(Contract):
    version: Identifier
    status: VersionStatus
    checksum: str
    author: Identifier
    created_at: AwareDatetime
    activated_at: AwareDatetime | None = None


class CheckResult(Contract):
    code: str
    passed: bool
    message: str


class ActivationReport(Contract):
    version: Identifier
    status: Literal["ACTIVE", "REJECTED"]
    report: list[CheckResult]


class ContextRequest(Contract):
    version: Identifier | None = None
    regime: Identifier | None = None
    task_class: Identifier | None = None
    lot_ids: list[Identifier] = Field(default_factory=list)
    query: Text | None = None


class CorpusFragment(Contract):
    fragment_id: Identifier
    text: Text
    source: Text
    date: Text
    score: Annotated[float, Field(ge=0, le=1)]


class Context(Contract):
    version: Identifier
    rules: list[Rule]
    facts: list[Fact]
    reference_data: dict[str, JsonValue]
    graph_slice: dict[str, JsonValue]
    corpus_fragments: list[CorpusFragment]


def compute_checksum(payload: VersionPayload) -> str:
    data = payload.model_dump(mode="json", exclude={"checksum"})
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
