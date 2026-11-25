from typing import Annotated

from pydantic import (
    Field,
    HttpUrl,
    IPvAnyAddress,
    RedisDsn,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from tender_orchestrator.models import Reviewer, Role

type Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
type Token = Annotated[SecretStr, Field(min_length=24)]
type Port = Annotated[int, Field(ge=1, le=65535)]
type PositiveTimeout = Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)]


class ReviewerCredential(Reviewer):
    token: Token


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TH_ORCHESTRATOR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_host: Name
    database_port: Port
    database_name: Name
    database_user: Name
    database_password: Annotated[SecretStr, Field(min_length=1)]
    database_timeout_seconds: Annotated[int, Field(ge=1, le=120)]
    database_max_connections: Annotated[int, Field(ge=1, le=64)]
    agent_url: HttpUrl
    agent_timeout_seconds: PositiveTimeout
    agent_max_attempts: Annotated[int, Field(ge=1, le=8)]
    agent_retry_delay_seconds: Annotated[float, Field(ge=0, le=60, allow_inf_nan=False)]
    review_role: Role
    review_timeout_seconds: Annotated[float, Field(gt=0, le=31_536_000, allow_inf_nan=False)]
    poll_interval_seconds: PositiveTimeout
    poll_batch_size: Annotated[int, Field(ge=1, le=1000)]
    redis_url: RedisDsn
    io_timeout_seconds: PositiveTimeout
    checkpoint_ttl_seconds: Annotated[int, Field(ge=1, le=604800)]
    kafka_brokers: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[^\s:/]+:[0-9]{1,5}$")]],
        Field(min_length=1),
    ]
    kafka_group_id: Name
    service_token: Token
    reviewers: Annotated[list[ReviewerCredential], Field(min_length=1)]
    bind_host: IPvAnyAddress
    port: Port
    artifact_token: Token | None = None
    artifact_max_bytes: Annotated[int, Field(ge=1, le=104857600)] | None = None

    @field_validator("agent_url")
    @classmethod
    def validate_agent_origin(cls, url: HttpUrl) -> HttpUrl:
        if url.username or url.password or url.query or url.fragment or url.path not in {None, "/"}:
            raise ValueError("expected an HTTP origin without credentials")
        return url

    @model_validator(mode="after")
    def validate_credentials(self) -> Settings:
        if (self.artifact_token is None) != (self.artifact_max_bytes is None):
            raise ValueError("artifact token and size limit must be configured together")
        tokens = [
            self.service_token.get_secret_value(),
            *(item.token.get_secret_value() for item in self.reviewers),
        ]
        if len(set(tokens)) != len(tokens):
            raise ValueError("service and reviewer tokens must be distinct")
        if any(not token.isascii() or not token.isprintable() or " " in token for token in tokens):
            raise ValueError("tokens must contain printable ASCII without spaces")
        if len({item.actor_id for item in self.reviewers}) != len(self.reviewers):
            raise ValueError("reviewer IDs must be distinct")
        return self
