from email.message import EmailMessage

import pytest

from tender_integrations.domain import MailRejected, parse_message


def message_bytes(*attachments: tuple[bytes, str, str, str]) -> bytes:
    message = EmailMessage()
    message["From"] = "Fixture <supplier@example.test>"
    message["Subject"] = "Tender package"
    message["Date"] = "Mon, 23 Nov 2026 10:00:00 +0300"
    message["Message-ID"] = "<fixture-01@example.test>"
    message.set_content("See the attachment.")
    for content, main, subtype, name in attachments:
        message.add_attachment(content, maintype=main, subtype=subtype, filename=name)
    return message.as_bytes()


def test_valid_documents_and_stable_order_independent_dedup() -> None:
    pdf = (b"%PDF-1.4 content", "application", "pdf", "docs.pdf")
    png = (b"\x89PNG\r\n\x1a\nimage", "image", "png", "image.png")
    rejected = (b"text", "text", "plain", "note.txt")
    first = parse_message("1", message_bytes(pdf, rejected, png), 256)
    second = parse_message("2", message_bytes(png, pdf), 256)
    assert first.message_id == "<fixture-01@example.test>"
    assert first.sender == "supplier@example.test"
    assert [item.name for item in first.attachments] == ["docs.pdf", "image.png"]
    assert first.idempotency_key("demo") == second.idempotency_key("demo")
    assert first.idempotency_key("demo") != second.idempotency_key("other")


@pytest.mark.parametrize(
    "attachment",
    [
        (b"invalid", "application", "pdf", "forged.pdf"),
        (b"%PDF-1.4 fake", "application", "pdf", "../unsafe.pdf"),
        (b"%PDF-" + b"x" * 256, "application", "pdf", "large.pdf"),
        (b"text", "text", "plain", "note.txt"),
    ],
)
def test_rejects_message_without_eligible_attachments(
    attachment: tuple[bytes, str, str, str],
) -> None:
    with pytest.raises(MailRejected, match="UNSUPPORTED_ATTACHMENT"):
        parse_message("1", message_bytes(attachment), 256)
