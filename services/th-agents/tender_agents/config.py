from typing import Annotated

from pydantic import Field, HttpUrl, IPvAnyAddress, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TH_AGENTS_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    model_gateway_url: HttpUrl
    model_gateway_timeout_seconds: Annotated[float, Field(gt=0, le=600)]
    public_url: HttpUrl
    bind_host: IPvAnyAddress
    port: Annotated[int, Field(ge=1, le=65535)]
    redis_url: RedisDsn
    redis_timeout_seconds: Annotated[float, Field(gt=0, le=10)]
    task_ttl_seconds: Annotated[int, Field(gt=0, le=604800)]
    task_lease_seconds: Annotated[int, Field(gt=0, le=1800)]
    task_wait_seconds: Annotated[float, Field(gt=0, le=600)]
    task_poll_seconds: Annotated[float, Field(gt=0, le=10)]

    @model_validator(mode="after")
    def validate_task_timing(self) -> Settings:
        if (
            self.task_lease_seconds
            <= self.model_gateway_timeout_seconds + self.redis_timeout_seconds
            or self.task_ttl_seconds <= self.task_lease_seconds
            or self.task_poll_seconds >= self.task_wait_seconds
        ):
            raise ValueError("invalid task lease, retention or polling duration")
        return self

    @field_validator("model_gateway_url", "public_url")
    @classmethod
    def validate_origin(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.username
            or value.password
            or value.query
            or value.fragment
            or value.path not in {None, "/"}
        ):
            raise ValueError("expected an HTTP origin without credentials")
        return value
