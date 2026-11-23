from enum import StrEnum


class ErrorCode(StrEnum):
    UNSUPPORTED_ATTACHMENT = "UNSUPPORTED_ATTACHMENT"
    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
    MAIL_UNAVAILABLE = "MAIL_UNAVAILABLE"
    MCP_STUB_ERROR = "MCP_STUB_ERROR"


class IntegrationError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
