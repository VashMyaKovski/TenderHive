import sys
from http.client import HTTPConnection

import uvicorn

from tender_docintel.api import create_app
from tender_docintel.config import Settings


def main() -> None:
    settings = Settings.model_validate({})
    command = sys.argv[1:]
    if command == ["healthcheck"]:
        host = str(settings.bind_host)
        if settings.bind_host.is_unspecified:
            host = "::1" if settings.bind_host.version == 6 else "127.0.0.1"
        connection = HTTPConnection(host, settings.port, timeout=settings.grpc_timeout_seconds)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    if command:
        raise SystemExit("Usage: python -m tender_docintel [healthcheck]")
    uvicorn.run(create_app(settings), host=str(settings.bind_host), port=settings.port)


if __name__ == "__main__":
    main()
