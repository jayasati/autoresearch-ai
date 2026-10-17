"""Errors of the report-quality-v1 pipeline (stage 30, S3)."""

from app.core.exceptions import AppError


class ReportQualityDisabled(AppError):
    """The opt-in pipeline was requested while its server-side gate is off."""

    status_code = 403
    code = "report_quality_disabled"
    message = (
        "The report-quality pipeline is not enabled on this server (REPORT_QUALITY_ENABLED is off)."
    )


class ReportQualityModeError(AppError):
    """report-quality-v1 builds on search_grounded retrieval and supports no other mode."""

    status_code = 422
    code = "report_quality_mode_unsupported"
    message = "The report-quality pipeline supports only the search_grounded mode."
