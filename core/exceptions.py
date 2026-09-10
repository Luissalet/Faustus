# core/exceptions.py
"""Custom exceptions for the application."""


def http_error_class(status_code: int) -> str:
    """The OBS-03 dotted code (`category.subcode`, `src/contracts/errors.py`)
    for a bare HTTP status code, with no exception object required.

    core/middleware.py's global error-response wiring (OBS-03) runs AFTER
    Starlette's own exception handling has already turned whatever was
    raised -- one of the four exceptions below, a plain `HTTPException` from
    any route, or a framework-level 404/405/422 no handler ever sees -- into
    a `Response`. By then the original exception is gone; the status code is
    all that is left, so this is deliberately a pure `int -> str` function
    rather than something that needs `from_exception`'s access to the raised
    exception (`src/contracts/errors.py`, used instead wherever the caller
    still has the exception in hand).

    Reuses `src.retry_policy.error_class_for` -- the SAME status ->
    OBS-03-code mapping `llm_core.py` already computes for provider-call
    errors (`src/retry_policy.py`, not owned by this lote) -- instead of a
    second, competing table for the same ten categories (rule 4). 409
    (conflict), 408 (timeout) and 499 (cancellation) used to need refining
    locally here because the shared table's default bucket
    (`schema.contract_violation`) did not distinguish them; `error_class_for`
    now names all three itself (Lote 44), so this module is a pure delegate
    with nothing left to add.
    """
    try:
        from src.retry_policy import error_class_for
    except Exception:
        return "unknown.unmapped_exception"
    return error_class_for(status=status_code)


class SessionNotFoundError(Exception):
    """Raised when a requested session is not found."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session '{session_id}' not found")

class InvalidFileUploadError(Exception):
    """Raised when a file upload fails validation."""
    def __init__(self, message: str, filename: str = None):
        self.filename = filename
        self.message = message
        super().__init__(message)

class LLMServiceError(Exception):
    """Raised when there is an error communicating with the LLM service."""
    def __init__(self, message: str, endpoint: str = None):
        self.endpoint = endpoint
        self.message = message
        super().__init__(message)

class WebSearchError(Exception):
    """Raised when there is an error with web search functionality."""
    def __init__(self, message: str, query: str = None):
        self.query = query
        self.message = message
        super().__init__(message)
