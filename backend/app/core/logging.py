"""
Logging configuration.

Configured once, at startup, before anything else logs. Two properties matter
for this project:

1. **Every line carries the request id.** When a research run eventually fans out
   across search, fetch, embedding and verification, a single failure needs to be
   traceable through dozens of log lines.
2. **Uvicorn's loggers are brought under the same configuration**, so access
   lines and application lines share one format instead of interleaving two.
"""

import logging
import sys

from app.utils.request_context import get_request_id

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RequestIdFilter(logging.Filter):
    """Injects ``request_id`` into every record so the format string can use it.

    A filter rather than an adapter: this way third-party libraries' log records
    get the field too, and the format string never raises KeyError.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; drop them and let records propagate to
    # the root handler above so the whole process logs in one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # We emit our own access line (with timing) in the middleware, so uvicorn's
    # duplicate is silenced rather than printed twice.
    logging.getLogger("uvicorn.access").disabled = True

    # Libraries that are chatty at INFO and tell us nothing we need.
    for noisy in ("httpx", "httpcore", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
