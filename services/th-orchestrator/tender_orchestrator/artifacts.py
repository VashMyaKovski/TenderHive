from hashlib import sha256
from re import fullmatch
from uuid import UUID

import httpx

from tender_orchestrator.models import ArtifactReceipt

_ARTIFACT_PATH = r"/api/v1/artifacts/([0-9a-f-]{36})/[0-9a-f-]{36}/(docx|pdf|xlsx)"


async def verify_package(
    http: httpx.AsyncClient,
    origin: str,
    token: str,
    job_id: UUID,
    receipt: ArtifactReceipt,
    max_bytes: int,
) -> bool:
    if len(receipt.artifacts) != 3:
        return False
    for item in receipt.artifacts:
        match = fullmatch(_ARTIFACT_PATH, item.file_path)
        if not match or match[1] != str(job_id) or match[2] != item.kind.lower():
            return False

    for item in receipt.artifacts:
        checksum = sha256()
        size = 0
        async with http.stream(
            "GET",
            origin.rstrip("/") + item.file_path,
            headers={"Authorization": "Bearer " + token},
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return False
                checksum.update(chunk)
        if size == 0 or checksum.hexdigest() != item.checksum.lower():
            return False
    return True
