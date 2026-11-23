from decimal import Decimal

from pydantic import Field, IPvAnyAddress
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLVER_", env_file=".env", env_file_encoding="utf-8"
    )

    kb_url: str = Field(min_length=1)
    modelgw_url: str = Field(min_length=1)
    redis_url: str = Field(min_length=1)
    active_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    margin_threshold_percent: Decimal = Field(gt=0)
    penalty_threshold_percent: Decimal = Field(gt=0)
    confidence_floor: float = Field(gt=0, le=1)
    cache_ttl_seconds: int = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0)
    bind_host: IPvAnyAddress
    port: int = Field(gt=0, lt=65536)
