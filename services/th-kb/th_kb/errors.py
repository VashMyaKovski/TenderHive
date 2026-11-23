from enum import StrEnum

from th_kb.models import CheckResult


class ErrorCode(StrEnum):
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    VERSION_EXISTS = "VERSION_EXISTS"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    INCOMPATIBLE_RULES = "INCOMPATIBLE_RULES"
    REGRESSION_DETECTED = "REGRESSION_DETECTED"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class KbError(Exception):
    def __init__(
        self, code: ErrorCode, message: str, report: list[CheckResult] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.report = report or []
