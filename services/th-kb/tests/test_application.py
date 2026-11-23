from datetime import UTC, datetime

import pytest

from tests.conftest import (
    FakeCache,
    FakeGraph,
    FakeVector,
    MemoryRepository,
    make_fact,
    make_payload,
    make_rule,
)
from th_kb.application import KnowledgeBase
from th_kb.config import Settings
from th_kb.errors import ErrorCode, KbError
from th_kb.models import ContextRequest, VersionStatus, compute_checksum
from th_kb.models import TestCase as CaseModel
from th_kb.validation import check_periods


def build_kb(settings: Settings) -> tuple[KnowledgeBase, MemoryRepository, FakeCache]:
    repository = MemoryRepository()
    cache = FakeCache()
    kb = KnowledgeBase(
        settings,
        repository,
        FakeGraph(),
        FakeVector(),
        cache,
        lambda: datetime(2026, 11, 23, tzinfo=UTC),
    )
    return kb, repository, cache


async def test_create_activate_and_context(settings: Settings) -> None:
    kb, repository, cache = build_kb(settings)
    payload = make_payload(
        facts=[make_fact()],
        test_cases=[
            CaseModel(
                case_id="case-1",
                input_facts={"margin_percent": 12},
                expected_rule_id="R-07",
            )
        ],
    )
    created = await kb.create_version(payload)
    assert created.status is VersionStatus.DRAFT
    assert repository.statuses[payload.version] is VersionStatus.DRAFT

    report = await kb.activate(payload.version)
    assert report.status == "ACTIVE"
    assert all(check.passed for check in report.report)
    assert cache.value == payload.version

    context = await kb.get_context(
        ContextRequest(lot_ids=["lot-1"], query="penalty terms", task_class="UC-01")
    )
    assert context.version == payload.version
    assert [rule.rule_id for rule in context.rules] == ["R-07"]
    assert context.corpus_fragments[0].source == "tender.pdf"
    assert context.graph_slice["edges"]


async def test_incompatible_rules_are_rejected(settings: Settings) -> None:
    kb, repository, _ = build_kb(settings)
    payload = make_payload(
        rules=[
            make_rule("R-12", "LOW_MARGIN", 50),
            make_rule("R-15", "OK", 10),
        ]
    )
    await kb.create_version(payload)
    report = await kb.activate(payload.version)
    assert report.status == "REJECTED"
    assert report.report[-1].code == ErrorCode.INCOMPATIBLE_RULES
    assert repository.statuses[payload.version] is VersionStatus.REJECTED
    assert repository.active is None


async def test_checksum_mismatch_is_rejected(settings: Settings) -> None:
    kb, _, _ = build_kb(settings)
    payload = make_payload().model_copy(update={"checksum": "f" * 64})
    with pytest.raises(KbError) as error:
        await kb.create_version(payload)
    assert error.value.code is ErrorCode.CHECKSUM_MISMATCH


async def test_regression_against_active_version(settings: Settings) -> None:
    kb, repository, _ = build_kb(settings)
    case = CaseModel(
        case_id="case-1", input_facts={"margin_percent": 12}, expected_consequence="MET"
    )
    active = make_payload("kb-v1", test_cases=[case])
    await kb.create_version(active)
    await kb.activate(active.version)

    candidate = make_payload(
        "kb-v2",
        rules=[make_rule(consequence="NOT_MET")],
        test_cases=[case],
    )
    await kb.create_version(candidate)
    report = await kb.activate(candidate.version)
    assert report.status == "REJECTED"
    assert report.report[-1].code == ErrorCode.REGRESSION_DETECTED
    assert repository.active == "kb-v1"


def test_periods_check() -> None:
    payload = make_payload(
        rules=[
            make_rule().model_copy(
                update={
                    "effective_from": datetime(2026, 11, 2, tzinfo=UTC),
                    "effective_to": datetime(2026, 11, 1, tzinfo=UTC),
                }
            )
        ]
    )
    with pytest.raises(KbError) as error:
        check_periods(payload, None)
    assert error.value.code is ErrorCode.INTEGRITY_ERROR


def test_checksum_is_stable_for_same_content() -> None:
    payload = make_payload()
    assert compute_checksum(payload) == payload.checksum
