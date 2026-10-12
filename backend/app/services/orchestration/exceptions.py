"""
Failures that belong to the pipeline itself, not to any one provider.

Both are honest stops. The spec rule they enforce: a run that cannot produce a
grounded, cited report **fails**, it does not return something that looks like one.
"""

from app.core.exceptions import AppError


class NoEvidenceError(AppError):
    """Retrieval and processing produced nothing a report could be grounded in.

    Either no source came back at all, or every text cleaned to nothing (papers with
    no abstract, empty snippets). A 422, not a 5xx: nothing upstream failed — the
    topic simply has no usable evidence through the current providers, and inventing
    a report anyway is exactly what this project exists to not do.
    """

    status_code = 422
    code = "no_evidence"
    message = "No usable text evidence was retrieved for this topic."


class UncitedReportError(AppError):
    """The model produced a report whose sentences cite nothing, twice.

    Treated as a failed run rather than returned: an uncited report is
    indistinguishable from the model answering out of its own weights, which is the
    MODEL_ONLY mode, not this one.
    """

    status_code = 502
    code = "uncited_report"
    message = "The model did not produce a cited report."
