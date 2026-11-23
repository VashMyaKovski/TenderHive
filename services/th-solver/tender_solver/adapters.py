import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from redis.asyncio import Redis

from tender_solver.models import KnowledgeContext, SolverResult
from tender_solver.ports import RiskAssessment


class HttpKnowledgeClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout

    async def context(
        self,
        version: str,
        regime: str | None,
        task_class: str | None,
        lot_ids: list[str],
        query: str | None,
    ) -> KnowledgeContext:
        params: dict[str, Any] = {"version": version}
        if regime is not None:
            params["regime"] = regime
        if task_class is not None:
            params["task_class"] = task_class
        if query is not None:
            params["query"] = query
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._url}/api/v1/context", params=params)
        response.raise_for_status()
        return KnowledgeContext.model_validate(response.json())


class HttpRiskClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout

    async def assess(
        self,
        fragment: str,
        kb_version: str,
        prompt_version: str,
        daily_percent: Decimal,
        threshold: Decimal,
    ) -> RiskAssessment:
        payload = {
            "job_id": str(uuid4()),
            "task": "risk_assessment",
            "context": [
                {"type": "public_text", "text": fragment},
                {
                    "type": "knowledge",
                    "rules_version": kb_version,
                    "penalty_daily_threshold_percent": str(threshold),
                },
                {"type": "penalty", "daily_percent": str(daily_percent)},
            ],
            "prompt_version": prompt_version,
            "mask": True,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._url}/api/v1/inference", json=payload)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        answer = data["answer"]
        return RiskAssessment(
            level=answer["hypothesis"]["risk_level"],
            summary=answer["hypothesis"]["summary"],
            confidence=float(data["confidence"]),
            human_reason=answer.get("human_reason"),
            rule_id=answer["rule_id"],
            trace_id=UUID(data["trace_id"]),
        )


class RedisResultCache:
    def __init__(self, url: str) -> None:
        self._client = Redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> SolverResult | None:
        try:
            value = await self._client.get(key)
        except Exception:
            return None
        if value is None:
            return None
        return SolverResult.model_validate(json.loads(str(value)))

    async def set(self, key: str, result: SolverResult, ttl_seconds: int) -> None:
        try:
            await self._client.set(key, result.model_dump_json(), ex=ttl_seconds)
        except Exception:
            return

    async def close(self) -> None:
        await self._client.aclose()
