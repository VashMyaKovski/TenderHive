from pydantic import Field, IPvAnyAddress
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INTEGRATIONS_", env_file=".env", env_file_encoding="utf-8"
    )

    imap_host: str = Field(min_length=1)
    imap_port: int = Field(gt=0, lt=65536)
    imap_user: str = Field(min_length=1)
    imap_password: str = Field(min_length=1)
    mailbox: str = Field(min_length=1)
    poll_seconds: int = Field(gt=0)
    storage_path: str = Field(min_length=1)
    postgres_dsn: str = Field(min_length=1)
    redis_url: str = Field(min_length=1)
    kafka_brokers: str = Field(min_length=1)
    kafka_topic: str = Field(min_length=1)
    idempotency_ttl_seconds: int = Field(gt=0)
    profile_ref: str = Field(min_length=1)
    kb_version: str = Field(min_length=1)
    counterparty_name: str = Field(min_length=1)
    bind_host: IPvAnyAddress
    port: int = Field(gt=0, lt=65536)
