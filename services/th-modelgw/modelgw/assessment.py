from typing import Literal

from modelgw.schemas import Knowledge, ModelRisk, Penalty, RiskAnswer


def assess_risk(
    hypothesis: ModelRisk,
    knowledge: Knowledge,
    penalty: Penalty,
    confidence_floor: float,
) -> tuple[RiskAnswer, float]:
    threshold_exceeded = penalty.daily_percent > knowledge.penalty_daily_threshold_percent
    conflict = (
        hypothesis.penalty_daily_percent is not None
        and hypothesis.penalty_daily_percent != penalty.daily_percent
    ) or (threshold_exceeded and hypothesis.risk_level != "high")
    confidence = 0.0 if conflict else hypothesis.confidence
    human_reason: Literal["penalty_above_threshold", "unclear_requirements"] | None = None
    if threshold_exceeded:
        human_reason = "penalty_above_threshold"
    elif hypothesis.risk_level == "high" or confidence < confidence_floor:
        human_reason = "unclear_requirements"

    return (
        RiskAnswer(
            hypothesis=hypothesis,
            source_ids=list(dict.fromkeys(hypothesis.sources)),
            rules_version=knowledge.rules_version,
            penalty_daily_percent=penalty.daily_percent,
            penalty_daily_threshold_percent=knowledge.penalty_daily_threshold_percent,
            threshold_exceeded=threshold_exceeded,
            conflict=conflict,
            human_reason=human_reason,
        ),
        confidence,
    )
