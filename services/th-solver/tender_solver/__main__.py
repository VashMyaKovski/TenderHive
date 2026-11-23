import asyncio
import sys
from http.client import HTTPConnection

import uvicorn

from tender_solver.adapters import HttpKnowledgeClient, HttpRiskClient, RedisResultCache
from tender_solver.api import create_app
from tender_solver.config import Settings
from tender_solver.solver import Solver


def _host(settings: Settings) -> str:
    if not settings.bind_host.is_unspecified:
        return str(settings.bind_host)
    return "::1" if settings.bind_host.version == 6 else "127.0.0.1"


async def _serve(settings: Settings, url_host: str) -> None:
    knowledge = HttpKnowledgeClient(settings.kb_url, settings.request_timeout_seconds)
    risk = HttpRiskClient(settings.modelgw_url, settings.request_timeout_seconds)
    cache = RedisResultCache(settings.redis_url)
    solver = Solver(settings, knowledge, risk, cache, settings.confidence_floor)
    config = uvicorn.Config(create_app(solver), host=url_host, port=settings.port, access_log=False)
    try:
        await uvicorn.Server(config).serve()
    finally:
        await cache.close()


def main() -> None:
    settings = Settings.model_validate({})
    host = _host(settings)
    if sys.argv[1:] == ["healthcheck"]:
        connection = HTTPConnection(host, settings.port, timeout=settings.request_timeout_seconds)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    asyncio.run(_serve(settings, str(settings.bind_host)))


if __name__ == "__main__":
    main()
