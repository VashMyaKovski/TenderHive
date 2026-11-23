from typing import Annotated, Literal

from cryptography.fernet import Fernet
from pydantic import (
    AliasChoices,
    Field,
    HttpUrl,
    IPvAnyAddress,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from modelgw.prompts import PROMPTS
from modelgw.schemas import Identifier, Task


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODEL_GW_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    nvidia_api_key: SecretStr | None = Field(validation_alias="NVIDIA_API_KEY")
    nvidia_base_url: HttpUrl = Field(validation_alias="NVIDIA_BASE_URL")
    chat_model: Identifier | None = Field(validation_alias="MODEL_RISK")
    embeddings_model: Identifier | None = Field(validation_alias="MODEL_EMBEDDING")
    vision_model: Identifier | None = Field(validation_alias="MODEL_VLM")
    embedding_input_type: Literal["query", "passage"] | None
    prompt_version: Identifier = Field(
        validation_alias=AliasChoices("PROMPT_VERSION", "MODEL_GW_PROMPT_VERSION")
    )
    postgres_dsn: SecretStr
    masking_key: SecretStr | None
    db_timeout_seconds: Annotated[int, Field(ge=2, le=60)]
    pii_masking_enabled: bool = Field(validation_alias="PII_MASKING_ENABLED")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        validation_alias="LOG_LEVEL"
    )
    otlp_endpoint: HttpUrl | None
    otlp_timeout_seconds: Annotated[float, Field(gt=0, le=60)]
    confidence_floor: Annotated[float, Field(gt=0, le=1)]
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = Field(
        validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "MODEL_GW_TIMEOUT_SECONDS")
    )
    max_retries: Annotated[int, Field(ge=0, le=2)] = Field(
        validation_alias=AliasChoices("LLM_MAX_RETRIES", "MODEL_GW_MAX_RETRIES")
    )
    retry_delay_seconds: Annotated[float, Field(gt=0, le=15)]
    max_completion_tokens: Annotated[int, Field(ge=64, le=8192)]
    redis_url: RedisDsn
    redis_timeout_seconds: Annotated[float, Field(gt=0, le=10)]
    cache_ttl_seconds: Annotated[int, Field(ge=1, le=86_400)] = Field(
        validation_alias=AliasChoices("CACHE_TTL_SECONDS", "MODEL_GW_CACHE_TTL_SECONDS")
    )
    bind_host: IPvAnyAddress
    port: Annotated[int, Field(ge=1, le=65535)]

    @field_validator("nvidia_api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if not secret or not secret.isascii() or any(char.isspace() for char in secret):
            raise ValueError("NVIDIA API key must be a nonempty token")
        return value

    @field_validator("nvidia_base_url")
    @classmethod
    def validate_provider_url(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.scheme != "https"
            or value.host != "integrate.api.nvidia.com"
            or value.port != 443
            or value.path not in {"/v1", "/v1/"}
            or value.username
            or value.password
            or value.query
            or value.fragment
        ):
            raise ValueError(
                "expected NVIDIA's HTTPS inference API at integrate.api.nvidia.com/v1/"
            )
        return value

    @field_validator("postgres_dsn")
    @classmethod
    def validate_postgres_dsn(cls, value: SecretStr) -> SecretStr:
        try:
            dsn = PostgresDsn(value.get_secret_value())
        except ValueError:
            raise ValueError("expected a PostgreSQL URL") from None
        if dsn.scheme not in {"postgresql", "postgres"} or dsn.path in {None, "/"}:
            raise ValueError("expected a PostgreSQL URL with a database name")
        return value

    @field_validator("masking_key")
    @classmethod
    def validate_masking_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            Fernet(value.get_secret_value())
        except ValueError:
            raise ValueError("expected a Fernet key") from None
        return value

    @field_validator("pii_masking_enabled")
    @classmethod
    def require_masking(cls, enabled: bool) -> bool:
        if not enabled:
            raise ValueError("masking must be enabled")
        return enabled

    @field_validator(
        "chat_model",
        "embeddings_model",
        "vision_model",
        "nvidia_api_key",
        "masking_key",
        "embedding_input_type",
        "otlp_endpoint",
        mode="before",
    )
    @classmethod
    def blank_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def require_keys_for_models(self) -> Settings:
        if any(self.models().values()) and (
            self.nvidia_api_key is None or self.masking_key is None
        ):
            raise ValueError(
                "NVIDIA_API_KEY and MODEL_GW_MASKING_KEY are required to enable a model"
            )
        return self

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
