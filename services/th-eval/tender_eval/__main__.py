import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from tender_eval.cases import DatasetError, load_dataset
from tender_eval.config import Settings
from tender_eval.fixtures import load_fixtures
from tender_eval.gateway import HttpGateway
from tender_eval.report import (
    CacheProfile,
    RunManifest,
    build_report,
    compare_reports,
    read_report,
    write_report,
)
from tender_eval.runner import run_cases
from tender_eval.telemetry import publish_metrics


async def run(settings: Settings, live: bool, cache_profile: CacheProfile) -> int:
    dataset = load_dataset(settings.dataset_path)
    cases = tuple(case for case in dataset.cases if case.live) if live else dataset.cases
    if not cases or len(cases) * settings.repeats > settings.max_attempts:
        raise DatasetError("selected scenarios are empty or exceed the configured attempt budget")
    if cache_profile == "isolated_cold" and settings.repeats != 1:
        raise DatasetError("an isolated cold run requires one repeat on a fresh gateway cache")
    fixtures = None if live else load_fixtures(settings.fixture_path, dataset.version, cases)
    manifest = RunManifest(
        run_id=uuid4(),
        started_at=datetime.now(UTC),
        mode="live" if live else "fixture",
        dataset_version=dataset.version,
        dataset_sha256=dataset.digest,
        fixture_sha256=fixtures.digest if fixtures else None,
        case_ids=tuple(case.case_id for case in cases),
        model_label=settings.model_label,
        prompt_version=settings.prompt_version,
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
        transport=fixtures.make_transport(settings.prompt_version) if fixtures else None,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        observations = await run_cases(
            HttpGateway(client, settings.timeout_seconds),
            cases,
            settings.prompt_version,
            settings.concurrency,
            settings.repeats,
        )
    report = build_report(manifest, observations)
    destination = write_report(settings.report_dir, report, observations)
    if settings.otlp_metrics_url is not None:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as telemetry:
            exported = await publish_metrics(
                telemetry, str(settings.otlp_metrics_url), settings.otlp_timeout_seconds, report
            )
        if not exported:
            print("th-eval metrics export failed; local report is available", file=sys.stderr)
    print(
        json.dumps(
            {
                "run_id": str(manifest.run_id),
                "mode": manifest.mode,
                "report": str(destination / "report.json"),
                "passed": report.aggregate.passed,
                "failed": report.aggregate.failed,
                "quality_gate_passed": report.quality_gate_passed,
            }
        )
    )
    return 0 if report.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tender_eval")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--live", action="store_true")
    run_parser.add_argument("--cache-profile", choices=("unverified", "isolated_cold", "warm"))
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("candidate", type=Path)
    args = parser.parse_args(sys.argv[1:] or ["run"])

    try:
        if args.command == "compare":
            print(
                json.dumps(
                    compare_reports(read_report(args.baseline), read_report(args.candidate)),
                    indent=2,
                )
            )
            return 0
        if args.cache_profile and not args.live:
            raise DatasetError("cache profiles apply only to explicitly requested live runs")
        cache_profile: CacheProfile = (
            (args.cache_profile or "unverified") if args.live else "not_applicable"
        )
        return asyncio.run(run(Settings.model_validate({}), args.live, cache_profile))
    except ValidationError as error:
        fields = sorted(
            {str(issue["loc"][0]) for issue in error.errors(include_input=False) if issue["loc"]}
        )
        print(f"th-eval invalid settings: {', '.join(fields)}", file=sys.stderr)
    except DatasetError as error:
        print(f"th-eval: {error}", file=sys.stderr)
    except OSError:
        print("th-eval could not write report artifacts", file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
