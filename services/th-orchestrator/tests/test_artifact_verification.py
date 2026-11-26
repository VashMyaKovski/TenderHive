from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

from tender_orchestrator.artifacts import verify_package
from tender_orchestrator.models import ArtifactReceipt


@pytest.mark.parametrize("bad_checksum", [False, True])
async def test_verify_downloaded_artifact_package(bad_checksum: bool) -> None:
    job, task = uuid4(), uuid4()
    body = b"synthetic artifact"
    checksum = sha256(body).hexdigest()
    values = []
    for kind in ("DOCX", "PDF", "XLSX"):
        values.append(
            {
                "artifact_id": str(uuid4()),
                "kind": kind,
                "file_path": f"/api/v1/artifacts/{job}/{task}/{kind.lower()}",
                "checksum": "0" * 64 if bad_checksum and kind == "PDF" else checksum,
                "kb_version": "demo",
                "version": "1",
            }
        )
    receipt = ArtifactReceipt.model_validate({"execution_id": str(task), "artifacts": values})

    def get_file(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "agent.invalid"
        assert request.headers["Authorization"] == "Bearer demo-token"
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(get_file)) as client:
        result = await verify_package(
            client, "http://agent.invalid", "demo-token", job, receipt, 100
        )
    assert result is not bad_checksum
