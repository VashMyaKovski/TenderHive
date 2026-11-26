import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from io import BytesIO
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile

_IMAGE_TYPES = {
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".tif": ("image/tiff", (b"II*\x00", b"MM\x00*")),
    ".tiff": ("image/tiff", (b"II*\x00", b"MM\x00*")),
    ".gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    ".bmp": ("image/bmp", (b"BM",)),
}
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class MailRejected(ValueError):
    """A message that requires review in the source mailbox."""


class MailUnavailable(RuntimeError):
    """The mailbox request failed and can be retried."""


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    mime: str
    content: bytes
    checksum: str


@dataclass(frozen=True, slots=True)
class IncomingMail:
    source_id: str
    message_id: str
    sender: str
    subject: str
    sent_at: datetime
    attachments: tuple[Attachment, ...]

    def idempotency_key(self, mailbox: str) -> str:
        digest = hashlib.sha256()
        # Framing protects the mailbox and Message-ID boundary. Sorting treats a resend
        # with reordered attachments as the same package without collapsing repeated files.
        for part in (mailbox, self.message_id, *sorted(item.checksum for item in self.attachments)):
            encoded = part.encode()
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.hexdigest()


def parse_message(source_id: str, raw: bytes, max_attachment_bytes: int) -> IncomingMail:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except RecursionError as error:
        raise MailRejected("INVALID_MESSAGE") from error
    if not isinstance(message, EmailMessage):
        raise MailRejected("INVALID_MESSAGE")
    headers = {}
    for field in ("Message-ID", "From", "Subject", "Date"):
        values = message.get_all(field, ())
        if len(values) != 1 or not (value := str(values[0]).strip()):
            raise MailRejected("INVALID_MESSAGE")
        if any(character in value for character in "\x00\r\n"):
            raise MailRejected("INVALID_MESSAGE")
        headers[field] = value

    try:
        sent_at = parsedate_to_datetime(headers["Date"])
    except (ValueError, OverflowError) as error:
        raise MailRejected("INVALID_MESSAGE") from error

    sent_at = sent_at.replace(tzinfo=sent_at.tzinfo or UTC).astimezone(UTC)
    _, sender = parseaddr(headers["From"])
    message_id = headers["Message-ID"]
    if (
        len(message_id) > 998
        or not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", message_id)
        or "@" not in sender
    ):
        raise MailRejected("INVALID_MESSAGE")

    attachments = tuple(
        attachment
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
        and (attachment := _attachment(part, max_attachment_bytes)) is not None
    )
    if not attachments:
        raise MailRejected("UNSUPPORTED_ATTACHMENT")
    return IncomingMail(source_id, message_id, sender, headers["Subject"], sent_at, attachments)


def _attachment(part: EmailMessage, max_bytes: int) -> Attachment | None:
    name = part.get_filename()
    if (
        not name
        or len(name.encode("utf-8", errors="replace")) > 255
        or re.search(r"[/\\:\x00-\x1f\x7f]", name)
    ):
        return None

    content = part.get_payload(decode=True)
    if not isinstance(content, bytes) or not 0 < len(content) <= max_bytes or part.defects:
        return None
    mime = _file_type(PurePath(name).suffix.lower(), content)
    if not mime or part.get_content_type() not in (mime, "application/octet-stream"):
        return None
    return Attachment(name, mime, content, hashlib.sha256(content).hexdigest())


def _file_type(suffix: str, content: bytes) -> str | None:
    if suffix == ".pdf" and content.startswith(b"%PDF-"):
        return "application/pdf"
    if suffix == ".docx" and _is_docx(content):
        return _DOCX
    if suffix == ".webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if suffix in _IMAGE_TYPES:
        mime, magic = _IMAGE_TYPES[suffix]
        if content.startswith(magic):
            return mime
    return None


def _is_docx(content: bytes) -> bool:
    try:
        with ZipFile(BytesIO(content)) as archive:
            return {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist())
    except BadZipFile:
        return False
