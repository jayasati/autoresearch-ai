"""Claim extraction's own failure."""

from app.core.exceptions import AppError


class ClaimExtractionFailed(AppError):
    """The model could not produce a valid extraction, twice.

    The "fail safely" half of the retry policy: after one retry the extraction stops
    with this error and **nothing is stored** — a partially parsed claim set would
    make every metric computed over it wrong in a way that looks like a finding.
    The report itself is untouched; extraction can be re-run.
    """

    status_code = 502
    code = "claim_extraction_failed"
    message = "The model did not produce a valid claim extraction."
