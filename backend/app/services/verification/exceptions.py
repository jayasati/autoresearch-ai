"""Verification's own failures."""

from app.core.exceptions import AppError


class VerificationUnavailable(AppError):
    """The run's mode cannot be verified yet.

    Verification retrieves evidence per claim from the run's vector collection,
    which only SEARCH_GROUNDED runs have. HYBRID and MODEL_ONLY runs will be
    verifiable when the benchmark stage needs them (hybrid by embedding its chunks
    on demand; model_only against retrieval it never performed, which is the whole
    point of measuring it) — saying "not yet" is honest, pretending is not.
    """

    status_code = 422
    code = "verification_unavailable"
    message = "This run's mode cannot be verified yet; only search_grounded runs can."


class NothingToVerify(AppError):
    """The run has no unverified claims — either extraction never ran, or every
    claim already has a verdict. The message says which."""

    status_code = 409
    code = "nothing_to_verify"
    message = "The run has no unverified claims."
