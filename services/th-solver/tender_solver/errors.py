from enum import StrEnum


class ErrorCode(StrEnum):
    KB_VERSION_NOT_FOUND = "KB_VERSION_NOT_FOUND"
    INCOMPLETE_INPUT = "INCOMPLETE_INPUT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INCOMPATIBLE_RULES = "INCOMPATIBLE_RULES"
    NO_SOLUTION = "NO_SOLUTION"


class SolverError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
