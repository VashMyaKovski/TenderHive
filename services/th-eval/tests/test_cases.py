import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tender_eval.cases import DatasetError, EvalCase, load_dataset

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "risk-v1.jsonl"


def test_risk_dataset_has_all_strata_and_live_cases() -> None:
    dataset = load_dataset(DATASET)

    assert dataset.version == "risk-v1"
    assert len(dataset.digest) == 64
    assert len({case.case_id for case in dataset.cases}) == len(dataset.cases)
    assert {case.stratum for case in dataset.cases} == {"typical", "boundary", "negative"}
    assert sum(case.live for case in dataset.cases) == 3


def test_duplicate_case_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.jsonl"
    header, case, *_ = DATASET.read_text(encoding="utf-8").splitlines()
    path.write_text(f"{header}\n{case}\n{case}\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="duplicate case ID at line 3"):
        load_dataset(path)


def test_invalid_case_does_not_leak_input(tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    header, *_ = DATASET.read_text(encoding="utf-8").splitlines()
    path.write_text(f'{header}\n{{"unexpected":"private marker"}}\n', encoding="utf-8")

    with pytest.raises(DatasetError, match="^invalid dataset case at line 2$"):
        load_dataset(path)


def test_live_cases_cannot_contain_unclassified_text() -> None:
    _, raw_case, *_ = DATASET.read_text(encoding="utf-8").splitlines()
    case = json.loads(raw_case)
    case["context"][0]["type"] = "public_text"

    with pytest.raises(ValidationError, match="only synthetic text"):
        EvalCase.model_validate(case)


def test_threshold_case_ethalon_must_agree_with_local_rule() -> None:
    _, raw_case, *_ = DATASET.read_text(encoding="utf-8").splitlines()
    case = json.loads(raw_case)
    case["expected"]["threshold_exceeded"] = True

    with pytest.raises(ValidationError, match="does not match the input"):
        EvalCase.model_validate(case)
