import argparse
import asyncio
import logging
import sys

import httpx
import uvicorn
from pydantic import ValidationError

from tender_integrations.api import create_app
from tender_integrations.config import Settings
from tender_integrations.postgres import Postgres


async def migrate(settings: Settings) -> None:
    database = Postgres(settings)
    try:
        await database.open()
        await database.migrate()
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="TenderHive mail integration service")
    parser.add_argument(
        "command", choices=("serve", "migrate", "healthcheck"), nargs="?", default="serve"
    )
    args = parser.parse_args()
    try:
        settings = Settings.model_validate({})
    except ValidationError as error:
        missing = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
        raise SystemExit(f"Invalid th-integrations settings ({missing})") from None

    logging.basicConfig(level=settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    if args.command == "migrate":
        asyncio.run(migrate(settings), loop_factory=loop_factory)
    elif args.command == "healthcheck":
        url = f"http://127.0.0.1:{settings.port}/api/v1/health/ready"
        try:
            httpx.get(url, timeout=settings.io_timeout_seconds, trust_env=False).raise_for_status()
        except httpx.HTTPError:
            raise SystemExit("th-integrations is unready") from None
    else:
        uvicorn.run(
            create_app(settings),
            host=str(settings.bind_host),
            port=settings.port,
            log_level=settings.log_level.lower(),
            access_log=False,
            timeout_graceful_shutdown=int(
                settings.mail_timeout_seconds + settings.io_timeout_seconds
            ),
        )


if __name__ == "__main__":
    main()
