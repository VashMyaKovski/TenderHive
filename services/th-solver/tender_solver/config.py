from decimal import Decimal
from typing import Annotated

from pydantic import Field, HttpUrl, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLVER_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    host: str
    port: Annotated[int, Field(gt=0, le=65535)]
    kb_url: HttpUrl
    kb_eval_token: SecretStr
    postgres_dsn: SecretStr
    redis_url: RedisDsn
    timeout_seconds: Annotated[int, Field(gt=0, le=120)]
    result_cache_ttl_seconds: Annotated[int, Field(gt=0, le=86400)]
    margin_threshold_percent: Annotated[Decimal, Field(allow_inf_nan=False, ge=0, le=100)]
