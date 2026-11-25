from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    IPvAnyAddress,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type Secret = Annotated[SecretStr, Field(min_length=1)]
type Port = Annotated[int, Field(ge=1, le=65535)]
type Seconds = Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)]


class ImapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: Text
    port: Port
    user: Text
    password: Secret
    folder: Text
    encryption: Literal["implicit_tls", "starttls", "plaintext"]


class ExchangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    graph_url: HttpUrl
    token_url: HttpUrl
    token_scope: Text
    client_id: Text
    client_secret: Secret
    mailbox_user: Text
    folder: Text

    @field_validator("graph_url", "token_url")
    @classmethod
    def secure_endpoint(cls, url: HttpUrl) -> HttpUrl:
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            raise ValueError("expected an HTTPS endpoint without embedded credentials")
        return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TH_INTEGRATIONS_",
        env_file=".env",
        env_nested_delimiter="__",
        env_parse_none_str="null",
        extra="ignore",
        hide_input_in_errors=True,
    )

    mailbox_id: Text
    imap: ImapConfig | None
    exchange: ExchangeConfig | None
    mail_poll_seconds: Seconds
    mail_timeout_seconds: Seconds
    max_message_bytes: Annotated[int, Field(ge=1024, le=64 * 1024 * 1024)]
    max_attachment_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)]
    database_dsn: PostgresDsn
    database_timeout_seconds: Annotated[int, Field(ge=2, le=120)]
    database_max_connections: Annotated[int, Field(ge=2, le=64)]
    redis_url: RedisDsn
    kafka_brokers: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[^\s:/]+:[0-9]{1,5}$")]],
        Field(min_length=1),
    ]
    io_timeout_seconds: Seconds
    outbox_poll_seconds: Seconds
    outbox_batch_size: Annotated[int, Field(ge=1, le=100)]
    outbox_lease_seconds: Seconds
    kb_version: Text
    profile_ref: Text
    document_base_url: HttpUrl
    document_token: Annotated[SecretStr, Field(min_length=24)]
    mcp_allowed_hosts: Annotated[list[Text], Field(min_length=1)]
    mcp_allowed_origins: list[Text]
    bind_host: IPvAnyAddress
    port: Port
    otlp_traces_endpoint: HttpUrl | None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @field_validator("document_base_url")
    @classmethod
    def document_origin(cls, url: HttpUrl) -> HttpUrl:
        if url.username or url.password or url.query or url.fragment or url.path not in {None, "/"}:
            raise ValueError("expected an HTTP origin without credentials")
        return url

    @model_validator(mode="after")
    def validate_mail_settings(self) -> Settings:
        if (self.imap is None) == (self.exchange is None):
            raise ValueError("configure exactly one of IMAP and Exchange")
        if self.max_attachment_bytes > self.max_message_bytes:
            raise ValueError("the maximum attachment size exceeds the maximum message size")
        if self.outbox_lease_seconds <= self.io_timeout_seconds + self.database_timeout_seconds:
            raise ValueError("outbox lease must exceed publish and database timeouts combined")
        return self
