import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx

from tender_eval.cases import Dataset, DatasetError
from tender_eval.config import Settings
from tender_eval.fixtures import load_fixtures
from tender_eval.gateway import HttpGateway
from tender_eval.report import CacheProfile, MetricReport, RunManifest, build_report
from tender_eval.runner import Observation, run_cases


async def execute(
    settings: Settings,
    dataset: Dataset,
    run_id: UUID,
    live: bool,
    cache_profile: CacheProfile,
    model_version: str,
    prompt_version: str,
) -> tuple[MetricReport, tuple[Observation, ...]]:
    cases = tuple(case for case in dataset.cases if case.live) if live else dataset.cases
    if not cases or len(cases) * settings.repeats > settings.max_attempts:
        raise DatasetError("selected scenarios are empty or exceed the configured attempt budget")
    if cache_profile == "isolated_cold" and settings.repeats != 1:
        raise DatasetError("an isolated cold run requires one repeat on a fresh gateway cache")
    fixtures = (
        None
        if live
        else await asyncio.to_thread(load_fixtures, settings.fixture_path, dataset.version, cases)
    )
    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(UTC),
        mode="live" if live else "fixture",
        dataset_version=dataset.version,
        dataset_sha256=dataset.digest,
        fixture_sha256=fixtures.digest if fixtures else None,
        case_ids=tuple(case.case_id for case in cases),
        model_label=model_version,
        prompt_version=prompt_version,
        cache_profile=cache_profile,
        repeats=settings.repeats,
        concurrency=settings.concurrency,
        timeout_seconds=settings.timeout_seconds,
        ece_buckets=settings.ece_buckets,
        min_risk_macro_f1=settings.min_risk_macro_f1,
    )
    async with httpx.AsyncClient(
        base_url=str(settings.gateway_url),
        timeout=settings.timeout_seconds,
        transport=fixtures.make_transport(prompt_version) if fixtures else None,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        observations = await run_cases(
            HttpGateway(client, settings.timeout_seconds),
            cases,
            prompt_version,
            settings.concurrency,
            settings.repeats,
        )
    return build_report(manifest, observations), observations
