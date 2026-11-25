import asyncio
import imaplib
import logging
import ssl
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from tender_integrations.config import ExchangeConfig, ImapConfig
from tender_integrations.domain import MailUnavailable

_LOG = logging.getLogger(__name__)


class ImapMailbox:
    def __init__(self, config: ImapConfig, timeout: float, max_message_bytes: int) -> None:
        self._config = config
        self._timeout = timeout
        self._max_message_bytes = max_message_bytes

    def open(self) -> AbstractAsyncContextManager[ImapSession]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[ImapSession]:
        # Let a running blocking operation complete before closing its connection.
        connect = asyncio.create_task(asyncio.to_thread(self._connect))
        try:
            connection = await asyncio.shield(connect)
        except asyncio.CancelledError:
            with suppress(imaplib.IMAP4.error, OSError):
                connection = await connect
                await asyncio.to_thread(connection.logout)
            raise
        except (imaplib.IMAP4.error, OSError) as error:
            raise MailUnavailable("MAIL_UNAVAILABLE") from error

        try:
            yield ImapSession(connection, self._max_message_bytes)
        except (imaplib.IMAP4.error, OSError) as error:
            raise MailUnavailable("MAIL_UNAVAILABLE") from error
        finally:
            with suppress(imaplib.IMAP4.error, OSError):
                await asyncio.to_thread(connection.logout)

    def _connect(self) -> imaplib.IMAP4:
        config = self._config
        if config.encryption == "implicit_tls":
            connection = imaplib.IMAP4_SSL(
                config.host,
                config.port,
                ssl_context=ssl.create_default_context(),
                timeout=self._timeout,
            )
        else:
            connection = imaplib.IMAP4(config.host, config.port, timeout=self._timeout)
        try:
            if config.encryption == "starttls":
                connection.starttls(ssl.create_default_context())
            connection.login(config.user, config.password.get_secret_value())
            quoted_folder = config.folder.replace("\\", "\\\\").replace('"', '\\"')
            status, _ = connection.select(f'"{quoted_folder}"')
            if status != "OK":
                raise imaplib.IMAP4.error("SELECT failed")
        except imaplib.IMAP4.error, OSError:
            connection.shutdown()
            raise
        return connection


class ImapSession:
    def __init__(self, connection: imaplib.IMAP4, max_message_bytes: int) -> None:
        self._connection = connection
        self._max_message_bytes = max_message_bytes

    async def unread(self) -> AsyncIterator[tuple[str, bytes]]:
        status, data = await self._uid("SEARCH", "UNSEEN")
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise MailUnavailable("MAIL_UNAVAILABLE")

        for uid in data[0].split():
            if not uid.isdigit():
                raise MailUnavailable("MAIL_UNAVAILABLE")
            source_id = uid.decode("ascii")
            status, parts = await self._uid(
                "FETCH", source_id, f"(BODY.PEEK[]<0.{self._max_message_bytes + 1}>)"
            )
            if status != "OK":
                raise MailUnavailable("MAIL_UNAVAILABLE")
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    if len(part[1]) > self._max_message_bytes:
                        _LOG.warning("mail skipped code=MESSAGE_TOO_LARGE")
                    else:
                        yield source_id, part[1]
                    break

    async def mark_processed(self, source_id: str) -> None:
        status, _ = await self._uid("STORE", source_id, "+FLAGS.SILENT", r"(\Seen)")
        if status != "OK":
            raise MailUnavailable("MAIL_UNAVAILABLE")

    async def _uid(self, command: str, *args: str):
        operation = asyncio.create_task(asyncio.to_thread(self._connection.uid, command, *args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            with suppress(imaplib.IMAP4.error, OSError):
                await operation
            raise


class _AccessToken(BaseModel):
    access_token: str


class _GraphMessage(BaseModel):
    id: str


class _GraphPage(BaseModel):
    value: list[_GraphMessage]


class ExchangeMailbox:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: ExchangeConfig,
        max_message_bytes: int,
        timeout: float,
    ) -> None:
        self._client = client
        self._config = config
        self._max_message_bytes = max_message_bytes
        self._timeout = timeout

    @asynccontextmanager
    async def open(self) -> AsyncIterator[ExchangeSession]:
        config = self._config
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(
                    str(config.token_url),
                    data={
                        "grant_type": "client_credentials",
                        "client_id": config.client_id,
                        "client_secret": config.client_secret.get_secret_value(),
                        "scope": config.token_scope,
                    },
                )
                response.raise_for_status()
                token = _AccessToken.model_validate_json(response.content).access_token
            yield ExchangeSession(
                self._client, config, token, self._max_message_bytes, self._timeout
            )
        except (httpx.HTTPError, ValueError, TimeoutError) as error:
            raise MailUnavailable("MAIL_UNAVAILABLE") from error


class ExchangeSession:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: ExchangeConfig,
        token: str,
        max_message_bytes: int,
        timeout: float,
    ) -> None:
        self._client = client
        self._origin = httpx.URL(str(config.graph_url))
        self._user_url = (
            f"{str(self._origin).rstrip('/')}/users/{quote(config.mailbox_user, safe='')}"
        )
        self._folder = quote(config.folder, safe="")
        self._headers = {"Authorization": f"Bearer {token}", "Prefer": 'IdType="ImmutableId"'}
        self._max_message_bytes = max_message_bytes
        self._timeout = timeout

    async def unread(self) -> AsyncIterator[tuple[str, bytes]]:
        page_url = f"{self._user_url}/mailFolders/{self._folder}/messages"
        params: dict[str, str] | None = {"$filter": "isRead eq false", "$select": "id"}
        while page_url:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(page_url, headers=self._headers, params=params)
                response.raise_for_status()
                page = response.json()
                messages = _GraphPage.model_validate(page).value
            for item in messages:
                if (raw := await self._mime(item.id)) is not None:
                    yield item.id, raw
            page_url = page.get("@odata.nextLink", "")
            if page_url:
                next_url = httpx.URL(page_url)
                if (next_url.scheme, next_url.host, next_url.port) != (
                    self._origin.scheme,
                    self._origin.host,
                    self._origin.port,
                ) or next_url.userinfo:
                    raise MailUnavailable("MAIL_UNAVAILABLE")
            params = None

    async def _mime(self, source_id: str) -> bytes | None:
        async with (
            asyncio.timeout(self._timeout),
            self._client.stream(
                "GET", f"{self._message_url(source_id)}/$value", headers=self._headers
            ) as response,
        ):
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > self._max_message_bytes:
                    _LOG.warning("mail skipped code=MESSAGE_TOO_LARGE")
                    return None
                content.extend(chunk)
            return bytes(content)

    async def mark_processed(self, source_id: str) -> None:
        async with asyncio.timeout(self._timeout):
            response = await self._client.patch(
                self._message_url(source_id), headers=self._headers, json={"isRead": True}
            )
            if response.status_code != 404:
                response.raise_for_status()

    def _message_url(self, source_id: str) -> str:
        return f"{self._user_url}/messages/{quote(source_id, safe='')}"
