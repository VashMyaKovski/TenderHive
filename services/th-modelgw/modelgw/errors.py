from enum import StrEnum


class Failure(StrEnum):
    INVALID_CONTEXT = "invalid_context"
    UNSAFE_CONTEXT = "unsafe_context"
    UNSUPPORTED_PROMPT = "unsupported_prompt_version"
    TASK_DISABLED = "task_disabled"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"


class GatewayError(Exception):
    def __init__(self, code: Failure) -> None:
        super().__init__(code.value)
        self.code = code
