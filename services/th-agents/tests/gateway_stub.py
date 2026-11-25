from typing import Any

from fastapi import FastAPI

from contracts.risk_task_v1 import HumanReason


def response_for(
    request: dict[str, Any],
    reason: HumanReason | None,
    confidence: float,
) -> dict[str, Any]:
    _, knowledge, penalty = request["context"]
    return {
        "job_id": request["job_id"],
        "confidence": confidence,
        "model_version": "fixture/model",
        "prompt_version": request["prompt_version"],
        "masked": True,
        "cached": False,
        "trace_id": request["trace_id"],
        "answer": {
            "kind": "risk_assessment",
            "hypothesis": {
                "summary": "Daily penalty applies to late delivery.",
                "risk_level": "high",
                "penalty_daily_percent": None,
                "confidence": 0.9,
                "sources": [1],
            },
            "source_ids": [1],
            "rules_version": knowledge["rules_version"],
            "rule_id": "R-RISK-04",
            "penalty_daily_percent": penalty["daily_percent"],
            "penalty_daily_threshold_percent": knowledge["penalty_daily_threshold_percent"],
            "threshold_exceeded": reason == "penalty_above_threshold",
            "conflict": confidence == 0.0,
            "human_reason": reason,
        },
    }


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/invoke")
    async def invoke(body: dict[str, Any]) -> dict[str, Any]:
        return response_for(body, "penalty_above_threshold", 0.9)

    return app
