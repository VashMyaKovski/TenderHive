from typing import Annotated

from pydantic import Field, HttpUrl, IPvAnyAddress, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from modelgw.prompts import PROMPTS
from modelgw.schemas import Identifier, Task


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODEL_GW_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    openrouter_api_key: SecretStr = Field(validation_alias="OPENROUTER_API_KEY")
    chat_model: Identifier = Field(validation_alias="OPENROUTER_MODEL_TEST")
    openrouter_url: HttpUrl
    synthetic_provider_collection: bool
    embeddings_model: Identifier | None
    vision_model: Identifier | None
    prompt_version: Identifier
    confidence_floor: Annotated[float, Field(gt=0, le=1)]
    timeout_seconds: Annotated[float, Field(gt=0, le=120)]
    max_retries: Annotated[int, Field(ge=0, le=2)]
    retry_delay_seconds: Annotated[float, Field(gt=0, le=15)]
    max_completion_tokens: Annotated[int, Field(ge=64, le=8192)]
    redis_url: RedisDsn
    redis_timeout_seconds: Annotated[float, Field(gt=0, le=10)]
    cache_ttl_seconds: Annotated[int, Field(ge=1, le=86_400)]
    bind_host: IPvAnyAddress
    port: Annotated[int, Field(ge=1, le=65535)]

    @field_validator("openrouter_api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or not secret.isascii() or any(char.isspace() for char in secret):
            raise ValueError("OpenRouter API key must be a nonempty token")
        return value

    @field_validator("openrouter_url")
    @classmethod
    def validate_provider_url(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.scheme != "https"
            or value.username
            or value.password
            or value.query
            or value.fragment
        ):
            raise ValueError("provider URL must be HTTPS without credentials, query or fragment")
        return value

    @field_validator("embeddings_model", "vision_model", mode="before")
    @classmethod
    def empty_model_is_disabled(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("prompt_version")
    @classmethod
    def validate_prompt_version(cls, version: str) -> str:
        if version not in PROMPTS:
            raise ValueError("prompt version is unavailable")
        return version

    def models(self) -> dict[Task, str | None]:
        return {
            "risk_assessment": self.chat_model,
            "document_draft": self.chat_model,
            "embeddings": self.embeddings_model,
            "vision": self.vision_model,
        }
