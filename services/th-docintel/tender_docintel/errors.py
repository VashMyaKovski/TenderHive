from typing import Literal

type ErrorCode = Literal[
    "UNSUPPORTED_FORMAT",
    "FILE_TOO_LARGE",
    "IMAGE_TOO_LARGE",
    "TOO_MANY_PAGES",
    "TOO_MANY_BLOCKS",
    "CORRUPTED_FILE",
    "OCR_UNAVAILABLE",
    "TEMPLATE_NOT_FOUND",
    "GRPC_UNAVAILABLE",
    "CORE_REJECTED",
    "CORE_FAILURE",
]


class DocIntelError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
