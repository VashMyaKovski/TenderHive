import asyncio
from hashlib import sha256
from os import getenv
from uuid import uuid4

import httpx
import pytest
from aiokafka import AIOKafkaConsumer
from aiokafka.structs import TopicPartition

from contracts.agent_task import AGENT_COMPLETED_TOPIC, AgentCompleted
from contracts.models import Artifact
from tender_agents.demo import send
from tests.test_roles import document_task, get_result


async def test_compose_replays_document_and_publishes_one_kafka_completion() -> None:
    origin = getenv("TH_AGENTS_TEST_ORIGIN")
    brokers = getenv("TH_AGENTS_TEST_KAFKA_BROKERS")
    token = getenv("TH_AGENTS_TEST_ARTIFACT_TOKEN")
    if not (origin and brokers and token):
        pytest.skip(
            "set TH_AGENTS_TEST_ORIGIN, TH_AGENTS_TEST_KAFKA_BROKERS and TH_AGENTS_TEST_ARTIFACT_TOKEN"
        )

    partition = TopicPartition(AGENT_COMPLETED_TOPIC, 0)
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as client,
        AIOKafkaConsumer(bootstrap_servers=brokers, enable_auto_commit=False) as consumer,
    ):
        consumer.assign([partition])
        await consumer.seek_to_end(partition)
        payload = document_task()
        first, replay = await asyncio.gather(
            send(client, origin, payload),
            send(client, origin, {**payload, "trace_id": str(uuid4())}),
        )
        result = get_result(first)
        assert result == get_result(replay)
        assert result.status == "DONE"

        async with asyncio.timeout(10):
            while True:
                message = await consumer.getone(partition)
                if message.key == str(result.job_id).encode():
                    break
        assert isinstance(message.value, bytes)
        event = AgentCompleted.model_validate_json(message.value)
        assert event.version == 1 and event.event_type == AGENT_COMPLETED_TOPIC
        assert event.payload == result and event.job_id == result.job_id
        assert not any(
            item.key == message.key
            for records in (await consumer.getmany(partition, timeout_ms=750)).values()
            for item in records
        )

        references = result.result["artifacts"]
        assert isinstance(references, list)
        assert len(references) == 3
        for item in references:
            artifact = Artifact.model_validate(item)
            response = await client.get(origin + artifact.file_path)
            assert response.status_code == 200
            assert sha256(response.content).hexdigest() == artifact.checksum
