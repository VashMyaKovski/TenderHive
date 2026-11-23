from pydantic import Field, IPvAnyAddress
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KB_", env_file=".env", env_file_encoding="utf-8")

    postgres_dsn: str = Field(min_length=1)
    neo4j_uri: str = Field(min_length=1)
    neo4j_user: str = Field(min_length=1)
    neo4j_password: str = Field(min_length=1)
    qdrant_url: str = Field(min_length=1)
    redis_url: str = Field(min_length=1)
    modelgw_url: str = Field(min_length=1)
    active_version: str = Field(min_length=1)
    test_set_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    collection: str = Field(min_length=1)
    cache_ttl_seconds: int = Field(gt=0)
    max_fragments: int = Field(gt=0)
    embed_timeout_seconds: float = Field(gt=0)
    qdrant_timeout_seconds: float = Field(gt=0)
    neo4j_timeout_seconds: float = Field(gt=0)
    bind_host: IPvAnyAddress
    port: int = Field(gt=0, lt=65536)
