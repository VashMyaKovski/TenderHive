import asyncio
import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError
from pydantic import ValidationError
from redis.exceptions import RedisError

from contracts.agent_task import AGENT_COMPLETED_TOPIC, AgentCompleted, AgentResult
from tender_agents.ledger import RedisRiskLedger

_LOG = logging.getLogger("uvicorn.error.agent_completions")


def completion_event(result: AgentResult) -> AgentCompleted:
    return AgentCompleted(
        event_id=uuid5(NAMESPACE_URL, f"tenderhive:agent-completion:v1:{result.task_id}"),
        version=1,
        event_type=AGENT_COMPLETED_TOPIC,
        job_id=result.job_id,
        occurred_at=datetime.now(UTC),
        payload=result,
    )


async def publish_completions(
    ledger: RedisRiskLedger,
    brokers: list[str],
    poll_seconds: float,
    timeout_seconds: float,
    lease_ms: int,
) -> None:
    while True:
        try:
            async with AIOKafkaProducer(
                bootstrap_servers=",".join(brokers),
                request_timeout_ms=int(timeout_seconds * 1000),
                acks="all",
            ) as producer:
                await _publish_ready(ledger, producer, poll_seconds, timeout_seconds, lease_ms)
        except (KafkaError, OSError, RedisError, TimeoutError) as error:
            _LOG.warning("completion_publisher retrying reason=%s", type(error).__name__)
            await asyncio.sleep(poll_seconds)


async def _publish_ready(
    ledger: RedisRiskLedger,
    producer: AIOKafkaProducer,
    poll_seconds: float,
    timeout_seconds: float,
    lease_ms: int,
) -> None:
    while True:
        lease = await ledger.claim_completion(lease_ms)
        if lease is None:
            await asyncio.sleep(poll_seconds)
            continue
        payload, _ = lease
        try:
            event = AgentCompleted.model_validate_json(payload)
        except ValidationError:
            _LOG.error("completion_publisher invalid stored event")
            await asyncio.sleep(poll_seconds)
            continue
        async with asyncio.timeout(timeout_seconds):
            await producer.send_and_wait(
                AGENT_COMPLETED_TOPIC,
                value=payload.encode(),
                key=str(event.job_id).encode(),
            )
        await ledger.acknowledge_completion(lease, event.payload.task_id)
