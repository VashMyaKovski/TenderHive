import asyncio
import sys
from http.client import HTTPConnection

import uvicorn

from tender_solver.api import create_app
from tender_solver.config import Settings
from tender_solver.storage import TraceStore


def main() -> None:
    settings = Settings.model_validate({})
    if sys.argv[1:] == ["healthcheck"]:
        host = "127.0.0.1" if settings.host == "0.0.0.0" else settings.host
        connection = HTTPConnection(host, settings.port, timeout=settings.timeout_seconds)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        if sys.argv[1:] == ["migrate"]:
            runner.run(
                TraceStore(
                    settings.postgres_dsn.get_secret_value(), settings.timeout_seconds
                ).migrate()
            )
        elif not sys.argv[1:]:
            config = uvicorn.Config(
                create_app(settings), host=settings.host, port=settings.port, access_log=False
            )
            runner.run(uvicorn.Server(config).serve())
        else:
            raise SystemExit("Usage: python -m tender_solver [migrate|healthcheck]")


if __name__ == "__main__":
    main()
