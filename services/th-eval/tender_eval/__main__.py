import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from tender_eval.cases import DatasetError, load_dataset
from tender_eval.config import ApiSettings, Settings
from tender_eval.execution import execute
from tender_eval.report import (
    CacheProfile,
    compare_reports,
    read_report,
    write_report,
)
from tender_eval.telemetry import publish_metrics


async def run(settings: Settings, live: bool, cache_profile: CacheProfile) -> int:
    report, observations = await execute(
        settings,
        load_dataset(settings.dataset_path),
        uuid4(),
        live,
        cache_profile,
        settings.model_label,
        settings.prompt_version,
    )
    manifest = report.run
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
    commands.add_parser("serve")
    commands.add_parser("migrate")
    commands.add_parser("healthcheck")
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("candidate", type=Path)
    args = parser.parse_args(sys.argv[1:] or ["run"])

    try:
        if args.command in {"serve", "migrate", "healthcheck"}:
            return api_command(args.command, ApiSettings.model_validate({}))
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


def api_command(command: str, settings: ApiSettings) -> int:
    import psycopg
    import uvicorn

    from tender_eval.api import create_app
    from tender_eval.storage import Store

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    store = Store(settings.postgres_dsn.get_secret_value(), settings.database_timeout_seconds)
    try:
        if command == "migrate":
            dataset = load_dataset(settings.dataset_path)
            asyncio.run(store.migrate(dataset, settings.kb_version), loop_factory=loop_factory)
        elif command == "healthcheck":
            host = settings.bind_host
            address = "127.0.0.1" if host.is_unspecified else str(host)
            if host.version == 6:
                address = "[::1]" if host.is_unspecified else f"[{host}]"
            with httpx.Client(timeout=settings.database_timeout_seconds, trust_env=False) as client:
                client.get(
                    f"http://{address}:{settings.port}/healthz",
                    headers={"X-Trace-Id": str(uuid4())},
                ).raise_for_status()
        else:
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings, store),
                    host=str(settings.bind_host),
                    port=settings.port,
                    access_log=False,
                )
            )
            asyncio.run(server.serve(), loop_factory=loop_factory)
    except psycopg.Error, TimeoutError, httpx.HTTPError:
        print("th-eval API or PostgreSQL unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
