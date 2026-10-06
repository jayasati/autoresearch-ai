"""
Per-request context.

Holds the correlation id for the request currently being served, so that any
log record emitted anywhere in the call stack can be tied back to the HTTP
request that caused it -- without threading a parameter through every function.

A ``ContextVar`` is the right tool here: it is isolated per task, so concurrent
requests never see each other's id.
"""

from contextvars import ContextVar

_UNSET = "-"

_request_id: ContextVar[str] = ContextVar("request_id", default=_UNSET)


def set_request_id(value: str) -> object:
    """Bind a request id to the current context. Returns a reset token."""
    return _request_id.set(value)


def get_request_id() -> str:
    """The current request id, or ``"-"`` outside a request."""
    return _request_id.get()


def reset_request_id(token: object) -> None:
    """Restore the previous value using the token from :func:`set_request_id`."""
    _request_id.reset(token)  # type: ignore[arg-type]
