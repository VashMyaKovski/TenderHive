import httpx
import pytest

from tender_integrations.config import ExchangeConfig
from tender_integrations.domain import MailUnavailable
from tender_integrations.mail import ExchangeMailbox


def config() -> ExchangeConfig:
    return ExchangeConfig.model_validate(
        {
            "graph_url": "https://graph.example.test/v1.0",
            "token_url": "https://auth.example.test/token",
            "token_scope": "https://graph.example.test/.default",
            "client_id": "fixture",
            "client_secret": "fake-secret",
            "mailbox_user": "mail@example.test",
            "folder": "inbox",
        }
    )


async def test_exchange_reads_mime_without_acknowledging_then_patches() -> None:
    requests: list[httpx.Request] = []
    mime = b"fixture MIME"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/token":
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "synthetic"})
        assert request.headers["Authorization"] == "Bearer synthetic"
        assert request.headers["Prefer"] == 'IdType="ImmutableId"'
        if path.endswith("/$value"):
            return httpx.Response(200, content=mime)
        if request.method == "PATCH":
            assert request.content == b'{"isRead":true}'
            return httpx.Response(200, json={"id": "fixture"})
        assert request.url.params["$filter"] == "isRead eq false"
        return httpx.Response(200, json={"value": [{"id": "fixture"}]})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        ExchangeMailbox(http, config(), 1024, 2).open() as session,
    ):
        assert [item async for item in session.unread()] == [("fixture", mime)]
        assert [request.method for request in requests] == ["POST", "GET", "GET"]
        await session.mark_processed("fixture")
        assert requests[-1].method == "PATCH"


async def test_exchange_refuses_cross_origin_pagination() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "synthetic"})
        assert request.url.host == "graph.example.test"
        return httpx.Response(
            200, json={"value": [], "@odata.nextLink": "https://other.example.test/next"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(MailUnavailable, match="MAIL_UNAVAILABLE"):
            async with ExchangeMailbox(http, config(), 1024, 2).open() as session:
                _ = [item async for item in session.unread()]
