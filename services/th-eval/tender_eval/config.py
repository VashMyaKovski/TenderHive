from pathlib import Path
from typing import Annotated, Literal

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, HttpUrl, IPvAnyAddress, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tender_eval.cases import Version


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TH_EVAL_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    dataset_path: Path
    fixture_path: Path
    report_dir: Path
    gateway_url: HttpUrl
    prompt_version: Version
    model_label: Version
    timeout_seconds: Annotated[float, Field(gt=0, le=600)]
    concurrency: Annotated[int, Field(ge=1, le=32)]
    repeats: Annotated[int, Field(ge=1, le=20)]
    max_attempts: Annotated[int, Field(ge=1, le=10_000)]
    ece_buckets: Annotated[int, Field(ge=2, le=50)]
    min_risk_macro_f1: Annotated[float, Field(ge=0, le=1)] | None
    otlp_metrics_url: HttpUrl | None
    otlp_timeout_seconds: Annotated[float, Field(gt=0, le=30)]

    @field_validator("min_risk_macro_f1", mode="before")
    @classmethod
    def blank_means_no_gate(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("otlp_metrics_url", mode="before")
    @classmethod
    def empty_telemetry_url(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("otlp_metrics_url")
    @classmethod
    def validate_metrics_url(cls, url: HttpUrl | None) -> HttpUrl | None:
        if url is not None and (url.username or url.password or url.query or url.fragment):
            raise ValueError("OTLP URL must not include credentials, query or fragment")
        return url

    @field_validator("gateway_url")
    @classmethod
    def validate_endpoint(cls, url: HttpUrl) -> HttpUrl:
        if url.username or url.password or url.query or url.fragment or url.path not in {None, "/"}:
            raise ValueError("gateway URL must be an origin without credentials")
        return url


class ApiSettings(Settings):
    postgres_dsn: Annotated[SecretStr, Field(min_length=1)]
    database_timeout_seconds: Annotated[int, Field(ge=2, le=120)]
    bind_host: IPvAnyAddress
    port: Annotated[int, Field(ge=1, le=65_535)]
    api_mode: Literal["fixture", "live"]
    kb_version: Version

    @field_validator("postgres_dsn")
    @classmethod
    def validate_dsn(cls, dsn: SecretStr) -> SecretStr:
        try:
            parameters = conninfo_to_dict(dsn.get_secret_value())
        except ProgrammingError:
            raise ValueError("invalid PostgreSQL DSN") from None
        if not all(parameters.get(field) for field in ("host", "port", "dbname", "user")):
            raise ValueError("PostgreSQL DSN requires host, port, database and user")
        return dsn
