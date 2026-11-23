import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx

from tender_orchestrator.config import Settings
from tender_orchestrator.models import JobView, StartJob

DEMO_PROFILE = "demo-profile"
JOBS_PATH = "/api/v1/jobs"


def demo_request(job_id: UUID, fixture: Path) -> StartJob:
    risk = json.loads(fixture.read_bytes())
    risk.update(job_id=str(job_id), task_id=str(uuid5(job_id, "risk_assessment")))
    return StartJob.model_validate(
        {
            "job": {
                "job_id": str(job_id),
                "idempotency_key": f"demo-{job_id}",
                "source": "web",
                "created_at": "2026-11-22T10:00:00Z",
                "status": "принято",
                "kb_version": risk["rules_version"],
                "lot_ids": ["лот-1"],
                "documents": [],
                "profile_ref": DEMO_PROFILE,
            },
            "risk": risk,
        }
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def run(
    action: str,
    origin: str,
    settings: Settings,
    request: StartJob,
) -> None:
    job_path = f"{JOBS_PATH}/{request.job.job_id}"
    service = bearer(settings.service_token.get_secret_value())
    async with httpx.AsyncClient(
        base_url=origin,
        timeout=settings.agent_timeout_seconds,
        trust_env=False,
    ) as client:
        if action == "start":
            response = await client.post(
                JOBS_PATH, json=request.model_dump(mode="json"), headers=service
            )
            response.raise_for_status()
            job = JobView.model_validate(response.json())
            async with asyncio.timeout(settings.agent_timeout_seconds):
                while job.phase in {"accepted", "assessing"}:
                    await asyncio.sleep(0.05)
                    state = await client.get(job_path, headers=service)
                    job = JobView.model_validate(state.raise_for_status().json())
            assert job.phase == "awaiting_review" and len(job.cards) == 1
            assert job.cards[0].reason_code == "PENALTY_ABOVE_THRESHOLD"
        else:
            response = await client.get(job_path, headers=service)
            response.raise_for_status()
            previous = JobView.model_validate(response.json())
            assert previous.phase == "awaiting_review" and len(previous.cards) == 1
            card = previous.cards[0]
            reviewer = next(
                item
                for item in settings.reviewers
                if item.role == card.role and DEMO_PROFILE in item.profile_refs
            )
            response = await client.post(
                f"{job_path}/cards/{card.card_id}/decision",
                json={
                    "decision_id": str(uuid4()),
                    "action": "continue",
                    "comment": "Риск рассмотрен",
                },
                headers=bearer(reviewer.token.get_secret_value()),
            )
            response.raise_for_status()
            job = JobView.model_validate(response.json())
            assert job.phase == "ready_for_next_stage" and job.risk_result == previous.risk_result
            assert job.cards[0].card_id == card.card_id and job.cards[0].decision is not None
        assert job.risk_attempts == 1 and job.risk_result is not None
        print(job.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "continue"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.action,
            args.url,
            Settings.model_validate({}),
            demo_request(args.job_id, args.fixture),
        )
    )


if __name__ == "__main__":
    main()
