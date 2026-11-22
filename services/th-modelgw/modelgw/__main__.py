import sys
from http.client import HTTPConnection

import uvicorn

from modelgw.api import create_app
from modelgw.config import Settings


def main() -> None:
    settings = Settings.model_validate({})
    if sys.argv[1:] == ["healthcheck"]:
        host = str(settings.bind_host)
        if settings.bind_host.is_unspecified:
            host = "::1" if settings.bind_host.version == 6 else "127.0.0.1"
        connection = HTTPConnection(host, settings.port, timeout=settings.timeout_seconds)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    uvicorn.run(create_app(settings), host=str(settings.bind_host), port=settings.port)


if __name__ == "__main__":
    main()
