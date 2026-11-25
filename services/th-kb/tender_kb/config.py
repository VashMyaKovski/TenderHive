from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    AnyUrl,
    Field,
    IPvAnyAddress,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    UrlConstraints,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from tender_kb.domain import Identifier

type PositiveSeconds = Annotated[int, Field(gt=0, le=300)]
type BoltUrl = Annotated[
    AnyUrl, UrlConstraints(allowed_schemes=["bolt", "bolt+s", "neo4j", "neo4j+s"])
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KB_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    postgres_dsn: PostgresDsn
    neo4j_uri: BoltUrl
    neo4j_user: Identifier
    neo4j_password: Annotated[SecretStr, Field(min_length=1)]
    neo4j_database: Identifier
    qdrant_url: AnyHttpUrl
    qdrant_collection: Identifier
    qdrant_api_key: SecretStr | None = None
    redis_url: RedisDsn | None = None
    eval_url: AnyHttpUrl
    eval_token: Annotated[SecretStr, Field(min_length=16)]
    test_set_version: Identifier
    embedding_mode: Literal["gateway", "fixture"]
    model_gw_url: AnyHttpUrl
    model_gw_prompt_version: Identifier
    vector_size: Annotated[int, Field(ge=8, le=8192)]
    database_timeout_seconds: PositiveSeconds
    io_timeout_seconds: PositiveSeconds
    eval_timeout_seconds: PositiveSeconds
    cache_ttl_seconds: PositiveSeconds
    max_fragments: Annotated[int, Field(gt=0, le=100)]
    bind_host: IPvAnyAddress
    port: Annotated[int, Field(gt=0, le=65535)]

    @field_validator("eval_url", "model_gw_url", "qdrant_url")
    @classmethod
    def validate_endpoint(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("service URL must omit credentials, queries and fragments")
        return url

    @field_validator("postgres_dsn")
    @classmethod
    def validate_postgres_dsn(cls, dsn: PostgresDsn) -> PostgresDsn:
        if dsn.scheme not in ("postgres", "postgresql") or dsn.path in (None, "/"):
            raise ValueError("PostgreSQL DSN must name a database")
        if not all(host["username"] for host in dsn.hosts()):
            raise ValueError("PostgreSQL DSN must name a user")
        return dsn

    @field_validator("eval_token")
    @classmethod
    def validate_eval_token(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value()
        if not value.isascii() or not all(char.isalnum() or char in "._-" for char in value):
            raise ValueError("evaluation token must use URL-safe ASCII characters")
        return token
