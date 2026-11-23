from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


@dataclass(frozen=True)
class Attachment:
    name: str
    content: bytes
    mime: str


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    sender: str
    subject: str
    received_at: datetime
    attachments: tuple[Attachment, ...]
    uid: str = ""


@dataclass(frozen=True)
class StoredDocument:
    file_id: UUID
    name: str
    mime: str
    checksum: str
    storage_path: str


class Job(Contract):
    job_id: UUID
    idempotency_key: str
    source: Literal["mail"] = "mail"
    external_ref: str
    created_at: datetime
    status: Literal["принято"] = "принято"
    kb_version: Identifier
    lot_ids: list[str] = Field(default_factory=list)
    documents: list[StoredDocument]
    profile_ref: Identifier
    trace_id: UUID


class McpResult(Contract):
    system: Literal["1c", "edo", "registry"]
    method: Literal["getOrganizationProfile", "getDocumentStatus", "checkCounterparty"]
    payload: dict[str, object]
