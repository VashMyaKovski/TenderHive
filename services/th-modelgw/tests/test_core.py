from base64 import b64decode, b64encode
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from modelgw.assessment import assess_risk
from modelgw.context import prepare_context
from modelgw.errors import Failure, GatewayError
from modelgw.privacy import redact_public_text
from modelgw.schemas import InferenceRequest, Knowledge, ModelRisk, Penalty, PublicImage, Task


def request_for(task: Task, context: list[dict[str, object]]) -> InferenceRequest:
    return InferenceRequest.model_validate(
        {
            "job_id": str(uuid4()),
            "task": task,
            "context": context,
            "prompt_version": "v1",
            "mask": True,
        }
    )


def test_masks_categories_and_omits_typed_private_fields() -> None:
    request = request_for(
        "document_draft",
        [
            {
                "type": "public_text",
                "text": "ФИО: Иванов Иван; ООО «Тест»; IBAN: BY00SECRET; "
                "УНП: 123456789; Цена: 1200 BYN; deliver chairs",
            },
            {"type": "sensitive_text", "category": "commercial", "text": "internal covenant"},
        ],
    )

    assert prepare_context(request).texts == (
        "[REDACTED_PERSONAL]; [REDACTED_ORGANIZATION]; [REDACTED_BANKING]; "
        "[REDACTED_REGISTRATION]; [REDACTED_COMMERCIAL]; deliver chairs",
    )


def test_blocks_suspect_text_without_a_reliable_replacement() -> None:
    with pytest.raises(GatewayError) as exc:
        redact_public_text("Неполный реквизит: IBAN BY00Z9")
    assert exc.value.code == Failure.UNSAFE_CONTEXT


def test_rejects_unclassified_context_and_mask_override() -> None:
    request = request_for("embeddings", [{"type": "public_text", "text": "public text"}])
    for update in ({"context": ["unclassified raw text"]}, {"mask": False}):
        with pytest.raises(ValidationError):
            InferenceRequest.model_validate({**request.model_dump(), **update})


def test_risk_requires_a_single_rules_snapshot_and_fact() -> None:
    request = request_for("risk_assessment", [{"type": "public_text", "text": "public terms"}])
    with pytest.raises(GatewayError) as exc:
        prepare_context(request)
    assert exc.value.code == Failure.INVALID_CONTEXT


@pytest.mark.parametrize(
    ("actual", "claimed", "level", "score", "expected_score", "human_reason"),
    [
        ("0.15", "0.1", "low", 0.9, 0.0, "penalty_above_threshold"),
        ("0.15", "0.15", "high", 0.9, 0.9, "penalty_above_threshold"),
        ("0.1", "0.1", "low", 0.4, 0.4, "unclear_requirements"),
        ("0.1", "0.1", "low", 0.8, 0.8, None),
    ],
)
def test_authoritative_rule_and_human_review(
    actual: str,
    claimed: str,
    level: str,
    score: float,
    expected_score: float,
    human_reason: str | None,
) -> None:
    knowledge = Knowledge(
        type="knowledge", rules_version="kb-v2", penalty_daily_threshold_percent=Decimal("0.1")
    )
    hypothesis = ModelRisk.model_validate(
        {
            "summary": "delivery",
            "risk_level": level,
            "penalty_daily_percent": claimed,
            "confidence": score,
            "sources": [1],
        }
    )
    penalty = Penalty(type="penalty", daily_percent=Decimal(actual))

    answer, confidence = assess_risk(hypothesis, knowledge, penalty, confidence_floor=0.6)

    assert answer.penalty_daily_percent == penalty.daily_percent
    assert answer.rules_version == "kb-v2"
    assert answer.human_reason == human_reason
    assert answer.threshold_exceeded == (Decimal(actual) > Decimal("0.1"))
    assert confidence == expected_score


def test_vision_reencodes_inline_image_without_metadata() -> None:
    png = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Author", "test-private-author")
    Image.new("RGB", (3, 3)).save(png, format="PNG", pnginfo=metadata)
    image = PublicImage(type="public_image", data=b64encode(png.getvalue()).decode("ascii"))
    request = request_for("vision", [image.model_dump()])

    image_url = prepare_context(request).image_url

    assert image_url is not None
    mime, data = image_url.split(",", 1)
    assert mime == "data:image/jpeg;base64"
    assert b"test-private-author" not in b64decode(data)
