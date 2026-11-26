from uuid import uuid4

from tender_docintel.proto import docintel_pb2 as pb


def test_extract_rpc_message_round_trips_protobuf() -> None:
    request = pb.ExtractRequirementsRequest(
        job_id=str(uuid4()),
        job_doc_id=str(uuid4()),
        template_set_version="ts-2026-11-1",
        blocks=[
            pb.Block(
                block_id=str(uuid4()),
                page=3,
                text="штраф 0,15 % в день",
                bbox=pb.BoundingBox(x0=10, y0=20, x1=400, y1=35),
                kind=pb.BLOCK_KIND_TEXT,
                ocr_confidence=0.62,
            )
        ],
    )

    decoded = pb.ExtractRequirementsRequest.FromString(request.SerializeToString())

    assert decoded == request
    assert decoded.blocks[0].bbox.x1 == 400
