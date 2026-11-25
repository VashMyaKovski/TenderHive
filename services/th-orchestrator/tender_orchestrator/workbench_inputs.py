import json
from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from contracts.agent_task import PositiveQuantity, Quantity
from contracts.models import Contract, Document
from contracts.risk_task_v1 import Percent

MAX_FILE_BYTES = 2 * 1024**2
MAX_UPLOAD_BYTES = 5 * 1024**2
MAX_BODY_BYTES = 4 * ((MAX_UPLOAD_BYTES + 2) // 3) + 32 * 1024
DEMO_MINIMUM_VALIDITY_WORKDAYS = 60
_MAX_ENCODED = 4 * ((MAX_FILE_BYTES + 2) // 3)
_MAX_PREVIEW_PAGES = 5
_MAX_PAGE_BYTES = 512 * 1024
_MIMES = {".pdf": "application/pdf", ".txt": "text/plain"}


class Attachment(Contract):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    content_type: Annotated[str, StringConstraints(max_length=100)]
    content_base64: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_ENCODED)]


class Facts(Contract):
    quantity: PositiveQuantity
    offer_validity_workdays: Annotated[int, Field(ge=0, le=3650)]
    unit_cost: Quantity
    offer_price: PositiveQuantity
    available_quantity: Quantity
    penalty_daily_percent: Percent | None = None


class RunInput(Contract):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)]
    documents: Annotated[list[Attachment], Field(min_length=1, max_length=5)]
    facts: Facts
    context: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class DecisionInput(Contract):
    decision: Literal["APPROVED", "REJECTED"]
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""
    card_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Upload:
    document: Document
    data: bytes


def load_uploads(run_id: UUID, incoming: RunInput, bound_url: str) -> list[Upload]:
    uploads: list[Upload] = []
    total = 0
    for index, item in enumerate(incoming.documents):
        name = item.name
        if any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("Имя файла содержит недопустимые символы.")
        suffix = PurePath(name).suffix.lower()
        mime = _MIMES.get(suffix)
        if mime is None:
            raise ValueError("Загрузите документ PDF или TXT.")
        if item.content_type not in {"", mime, "application/octet-stream"}:
            raise ValueError("Тип файла не соответствует расширению.")
        try:
            data = b64decode(item.content_base64, validate=True)
        except Base64Error, ValueError:
            raise ValueError("Не удалось прочитать переданные байты файла.") from None
        total += len(data)
        if not data or len(data) > MAX_FILE_BYTES or total > MAX_UPLOAD_BYTES:
            raise ValueError("Размер файла или суммарный размер вложений превышает лимит.")
        if suffix == ".txt":
            try:
                if not data.decode("utf-8-sig").strip() or b"\0" in data:
                    raise ValueError("TXT-файл пуст или имеет неверный формат.")
            except UnicodeDecodeError:
                raise ValueError("TXT-файл должен быть сохранён в UTF-8.") from None
        elif not data.startswith(b"%PDF-"):
            raise ValueError("У PDF-файла неверный заголовок.")
        uploads.append(_upload(run_id, f"attachment:{index}", bound_url, name, mime, data))
    return uploads


def normalization_record(run_id: UUID, incoming: RunInput, bound_url: str) -> Upload:
    data = json.dumps(
        {
            "source": "Ручной ввод оператора и демонстрационный профиль",
            "title": incoming.title,
            "public_excerpt": incoming.context,
            "operator_facts": incoming.facts.model_dump(mode="json"),
            "demo_minimum_offer_validity_workdays": DEMO_MINIMUM_VALIDITY_WORKDAYS,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    return _upload(
        run_id, "normalization", bound_url, "normalized-input.json", "application/json", data
    )


def _upload(run_id: UUID, key: str, bound_url: str, name: str, mime: str, data: bytes) -> Upload:
    file_id = uuid5(run_id, key)
    path = f"/api/workbench/runs/{run_id}/documents/{file_id}"
    return Upload(
        document=Document(
            file_id=file_id,
            name=name,
            mime=mime,
            checksum=sha256(data).hexdigest(),
            storage_path=f"{bound_url.rstrip('/')}{path}",
        ),
        data=data,
    )


def has_readable_text(upload: Upload) -> bool:
    if upload.document.mime == "text/plain":
        return True
    try:
        reader = PdfReader(BytesIO(upload.data), strict=False)
        if reader.is_encrypted:
            raise ValueError("Снимите пароль с PDF перед загрузкой.")
        for page in reader.pages[:_MAX_PREVIEW_PAGES]:
            content = page.get_contents()
            if (
                content is not None
                and len(content.get_data()) <= _MAX_PAGE_BYTES
                and page.extract_text().strip()
            ):
                return True
    except PdfReadError, RecursionError:
        raise ValueError("Не удалось прочитать PDF. Проверьте, что файл открывается.") from None
    return False


def decimal_text(value: Decimal) -> str:
    return format(value, "f")
