"""Identify an unfinished response from the transport's typed evidence."""


def interrupted_response(error):
    return (
        isinstance(error, dict)
        and error.get("partial") is True
        and error.get("status") in {502, 504}
        and error.get("error_class") in {
            "transport.incomplete_stream", "transport.connection_reset",
            "timeout.deadline_exceeded",
        }
    )


def temporary_service_outage(error):
    """The transport explicitly identified an unavailable, retryable service."""
    return (isinstance(error, dict) and error.get("status") == 503
            and error.get("retryable") is True
            and ("partial" not in error or error.get("partial") is False))
