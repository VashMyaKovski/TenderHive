from typing import Annotated

from pydantic import Field, IPvAnyAddress, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.docintel_v1 import TemplateVersion

type Port = Annotated[int, Field(ge=1, le=65535)]
type PositiveSeconds = Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCINTEL_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    grpc_host: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    grpc_port: Port
    grpc_timeout_seconds: PositiveSeconds
    grpc_max_message_bytes: Annotated[int, Field(ge=1024, le=100_000_000)]
    max_file_mb: Annotated[int, Field(ge=1, le=1024)]
    max_uncompressed_mb: Annotated[int, Field(ge=1, le=4096)]
    max_pages: Annotated[int, Field(ge=1, le=10_000)]
    max_blocks: Annotated[int, Field(ge=1, le=1_000_000)]
    max_image_pixels: Annotated[int, Field(ge=1, le=500_000_000)]
    ocr_lang: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    ocr_dpi: Annotated[int, Field(ge=72, le=600)]
    chunk_size: Annotated[int, Field(ge=100, le=12000)]
    text_layer_min_chars: Annotated[int, Field(ge=1, le=100_000)]
    confidence_floor: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    table_sum_tolerance: Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]
    template_set_version: TemplateVersion
    bind_host: IPvAnyAddress
    port: Port

    @field_validator("grpc_host")
    @classmethod
    def validate_grpc_host(cls, host: str) -> str:
        if "://" in host or "/" in host or any(char.isspace() for char in host):
            raise ValueError("gRPC host must be a hostname or IP address")
        return host

    @field_validator("ocr_lang")
    @classmethod
    def validate_ocr_languages(cls, value: str) -> str:
        if any(not (char.isalnum() or char in "+_.-") for char in value):
            raise ValueError("OCR language list contains unsupported characters")
        return value

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def max_uncompressed_bytes(self) -> int:
        return self.max_uncompressed_mb * 1024 * 1024

    @property
    def grpc_target(self) -> str:
        return f"{self.grpc_host}:{self.grpc_port}"
