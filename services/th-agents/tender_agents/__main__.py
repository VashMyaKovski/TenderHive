import logging
import sys
from http.client import HTTPConnection

import uvicorn

from tender_agents.api import create_app
from tender_agents.config import Settings


def main() -> None:
    settings = Settings.model_validate({})
    host = str(settings.bind_host)
    if sys.argv[1:] == ["healthcheck"]:
        if settings.bind_host.is_unspecified:
            host = "::1" if settings.bind_host.version == 6 else "127.0.0.1"
        connection = HTTPConnection(
            host, settings.port, timeout=settings.model_gateway_timeout_seconds
        )
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    # The SDK's own diagnostic logs can include A2A payloads and parser exceptions.
    sdk_logs = logging.getLogger("a2a")
    sdk_logs.addHandler(logging.NullHandler())
    sdk_logs.propagate = False
    uvicorn.run(create_app(settings, None), host=host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
