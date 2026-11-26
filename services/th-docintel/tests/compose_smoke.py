import json
import os
from io import BytesIO
from typing import cast
from uuid import uuid4

import httpx
from docx import Document
from PIL import Image, ImageDraw, ImageFont


def upload(url: str, filename: str, content_type: str, content: bytes) -> dict[str, object]:
    job_id = uuid4()
    file_id = uuid4()
    trace_id = uuid4()
    response = httpx.post(
        f"{url}/api/v1/extract",
        data={
            "job_id": str(job_id),
            "file_id": str(file_id),
            "template_set_version": "ts-2026-11-1",
        },
        files={"file": (filename, content, content_type)},
        headers={"X-Trace-Id": str(trace_id)},
        timeout=60,
        trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    assert payload["job_id"] == str(job_id)
    assert payload["trace_id"] == str(trace_id)
    return payload


def docx_fixture() -> bytes:
    document = Document()
    document.add_paragraph("Пеня составляет 0,15 % в день, но не более 10 %")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def scan_fixture() -> bytes:
    image = Image.new("RGB", (1500, 220), "white")
    draw = ImageDraw.Draw(image)
    draw.text((35, 65), "0.15% per day", font=ImageFont.load_default(size=64), fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def main() -> None:
    url = os.environ["DOCINTEL_E2E_URL"].rstrip("/")
    document_result = upload(
        url,
        "penalty.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        docx_fixture(),
    )
    requirements = cast(list[dict[str, object]], document_result["requirements"])
    document_numbers = cast(list[dict[str, object]], document_result["numbers"])
    requirement = requirements[0]
    assert document_result["status"] == "OK"
    assert requirement["value"] == "0.0015"
    assert requirement["unit"] == "fraction_per_day"
    assert any(
        item["value"] == "0.1" and item["role"] == "upper_bound" for item in document_numbers
    )

    scan_result = upload(url, "scan.png", "image/png", scan_fixture())
    scan_numbers = cast(list[dict[str, object]], scan_result["numbers"])
    assert any(item["value"] == "0.0015" and item["kind"] == "percent" for item in scan_numbers)

    print(
        json.dumps(
            {
                "docx_status": document_result["status"],
                "docx_penalty": requirement["value"],
                "scan_status": scan_result["status"],
                "scan_percent": "0.0015",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
