"""
Exception hierarchy for SAP SuccessFactors Integration.
"""


class SFIntegrationError(Exception):
    """Base exception for all SuccessFactors integration errors."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class SFAuthenticationError(SFIntegrationError):
    """Raised when authentication (OAuth2 SAML or Basic) fails."""
    pass


class SFODataError(SFIntegrationError):
    """Raised when SuccessFactors OData API returns an error response."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        sf_message: str | None = None,
        details: dict | None = None,
    ) -> None:
        merged_details = details or {}
        if status_code:
            merged_details["status_code"] = status_code
        if error_code:
            merged_details["sf_error_code"] = error_code
        if sf_message:
            merged_details["sf_message"] = sf_message

        super().__init__(message, details=merged_details)
        self.status_code = status_code
        self.error_code = error_code
        self.sf_message = sf_message


class SFPayloadValidationError(SFIntegrationError):
    """Raised when candidate resume or upsert payload fails validation."""
    pass


class SFVerificationError(SFIntegrationError):
    """Raised when post-upsert verification fails to confirm attachment persistence."""
    pass


class SFWriteOnceViolationError(SFIntegrationError):
    """Raised when attempting to overwrite an already populated Cust_Candidate_Resume."""
    pass


class SFWatermarkError(SFIntegrationError):
    """Raised when reading or persisting watermark state fails."""
    pass
