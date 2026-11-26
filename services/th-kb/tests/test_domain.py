from datetime import date

import pytest
from pydantic import ValidationError

from tender_kb.domain import ContextSelection, VersionUpload, integrity_issues, matching_rules


def _version(**changes: object) -> VersionUpload:
    data = {
        "version": "kb-demo",
        "rules": [],
        "facts": [],
        "test_cases": [],
        "reference_data": {},
        "checksum": "0" * 64,
        **changes,
    }
    return VersionUpload.model_validate(data)


def test_checksum_depends_on_supplied_fields_and_ignores_object_key_order() -> None:
    version = _version(reference_data={"order": 1, "kind": "test"})
    reordered = _version(reference_data={"kind": "test", "order": 1})
    assert version.digest() == reordered.digest()
    assert (
        version.digest()
        != _version(reference_data={"kind": "test", "order": 1}, corpus_fragments=[]).digest()
    )


def test_conflicting_rule_predicates_are_detected_for_overlapping_scopes() -> None:
    shared = {
        "priority": 50,
        "version": 1,
        "scope": {"kind": "license", "regime": "public"},
    }
    rules = [
        {
            **shared,
            "rule_id": "R-1",
            "condition": {"field": "level", "op": "gte", "value": 2},
            "consequence": {"status": "MET", "reason": "Enough."},
        },
        {
            **shared,
            "rule_id": "R-2",
            "condition": {"field": "level", "op": "eq", "value": 2},
            "consequence": {"status": "NOT_MET", "reason": "Insufficient."},
        },
    ]
    snapshot = _version(rules=rules)
    assert [issue.code for issue in integrity_issues(snapshot)] == ["INCOMPATIBLE_RULES"]
    assert not matching_rules(
        snapshot.rules,
        ContextSelection(
            lot_ids=(), regime="private", task_class=None, query=None, as_of=date(2026, 1, 1)
        ),
    )
    rules[1]["valid_from"] = "2026-11-22"
    rules[0]["valid_until"] = "2026-11-21"
    assert not integrity_issues(_version(rules=rules))


def test_relationship_types_periods_and_duplicate_case_ids_are_validated() -> None:
    case = {"case_id": "test-alpha", "input": {}, "expected": {}}
    snapshot = _version(
        graph_slice={
            "nodes": [{"node_id": "item-1", "kind": "position", "lot_id": "missing-lot"}],
            "edges": [{"source": "item-1", "target": "missing", "relation": "depends_on"}],
        },
        facts=[
            {
                "fact_id": "fact-1",
                "key": "verified",
                "value": True,
                "value_type": "number",
                "source": "Registry",
                "valid_from": "2026-11-21",
            },
            {
                "fact_id": "fact-2",
                "key": "verified",
                "value": False,
                "source": "Registry",
                "valid_until": "2026-11-21",
            },
        ],
        test_cases=[case, case],
    )
    issues = integrity_issues(snapshot)
    assert len(issues) == 5
    assert {issue.code for issue in issues} == {"INTEGRITY_ERROR"}
    assert any("overlapping effective periods" in issue.message for issue in issues)


def test_unknown_rule_fields_do_not_pass_the_upload_contract() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _version(untyped_rules=[])
