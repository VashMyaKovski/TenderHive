import asyncio
import sys
from http.client import HTTPConnection

import uvicorn

from modelgw.api import create_app, journal_for
from modelgw.config import Settings
from modelgw.errors import GatewayError


def main() -> None:
    settings = Settings.model_validate({})
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    if sys.argv[1:] == ["migrate"]:
        try:
            asyncio.run(journal_for(settings).migrate(), loop_factory=loop_factory)
        except GatewayError as error:
            raise SystemExit(f"Model gateway migration failed ({error.code}).") from None
        return
    if sys.argv[1:] == ["healthcheck"]:
        host = str(settings.bind_host)
        if settings.bind_host.is_unspecified:
            host = "::1" if settings.bind_host.version == 6 else "127.0.0.1"
        connection = HTTPConnection(host, settings.port, timeout=settings.timeout_seconds)
        try:
            connection.request("GET", "/readyz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=str(settings.bind_host),
            port=settings.port,
            log_level=settings.log_level.lower(),
        )
    )
    asyncio.run(server.serve(), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
