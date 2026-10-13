"""Verification's own failures."""

from app.core.exceptions import AppError


class NothingToVerify(AppError):
    """The run has no unverified claims — either extraction never ran, or every
    claim already has a verdict. The message says which."""

    status_code = 409
    code = "nothing_to_verify"
    message = "The run has no unverified claims."
