import asyncio
import logging
import sys
from datetime import UTC, datetime
from http.client import HTTPConnection

import uvicorn

from tender_orchestrator.api import create_app
from tender_orchestrator.config import Settings
from tender_orchestrator.db import Database, connection_info, migrate, migration_scripts


def main() -> None:
    settings = Settings.model_validate({})
    command = sys.argv[1:]
    host = str(settings.bind_host)
    if command == ["healthcheck"]:
        if settings.bind_host.is_unspecified:
            host = "::1" if settings.bind_host.version == 6 else "127.0.0.1"
        connection = HTTPConnection(host, settings.port, timeout=settings.database_timeout_seconds)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    if command == ["migrate"]:
        database = Database(connection_info(settings), settings.database_max_connections)
        asyncio.run(migrate(database, migration_scripts()), loop_factory=loop_factory)
        return
    if command:
        raise SystemExit("Usage: python -m tender_orchestrator [migrate|healthcheck]")

    sdk_logs = logging.getLogger("a2a")
    sdk_logs.addHandler(logging.NullHandler())
    sdk_logs.propagate = False
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, lambda: datetime.now(UTC), None),
            host=host,
            port=settings.port,
            access_log=False,
        )
    )
    asyncio.run(server.serve(), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
