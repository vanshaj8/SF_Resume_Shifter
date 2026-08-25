"""Client package for SAP SuccessFactors OData API."""
from client.auth import (
    BaseAuthProvider,
    BasicAuthProvider,
    MockAuthProvider,
    OAuth2SAMLBearerAuthProvider,
    create_auth_provider,
)
from client.exceptions import (
    SFAuthenticationError,
    SFIntegrationError,
    SFODataError,
    SFPayloadValidationError,
    SFVerificationError,
    SFWatermarkError,
    SFWriteOnceViolationError,
)

__all__ = [
    "BaseAuthProvider",
    "BasicAuthProvider",
    "MockAuthProvider",
    "OAuth2SAMLBearerAuthProvider",
    "create_auth_provider",
    "SFIntegrationError",
    "SFAuthenticationError",
    "SFODataError",
    "SFPayloadValidationError",
    "SFVerificationError",
    "SFWatermarkError",
    "SFWriteOnceViolationError",
]
