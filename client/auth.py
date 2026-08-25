"""
Authentication Manager for SAP SuccessFactors OData APIs.
Supports:
- OAuth 2.0 SAML 2.0 Bearer Assertion Flow (SAP SuccessFactors Standard)
- Basic Authentication (user@company_id:password)
- Mock Auth (for offline development and unit tests)
"""

from __future__ import annotations

import base64
import datetime
import logging
import time
import uuid
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from client.exceptions import SFAuthenticationError
from config.settings import AppConfig

logger = logging.getLogger("ResumeShifter.Auth")


class BaseAuthProvider(ABC):
    """Abstract authentication provider interface."""

    @abstractmethod
    def get_auth_headers(self) -> dict[str, str]:
        """Return HTTP headers required for authenticated requests."""
        pass

    @abstractmethod
    def invalidate(self) -> None:
        """Invalidate any cached tokens/credentials."""
        pass


class MockAuthProvider(BaseAuthProvider):
    """Mock auth provider for local testing and integration dry-runs."""

    def __init__(self, token: str = "mock_sf_bearer_token_xyz") -> None:
        self.token = token

    def get_auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    def invalidate(self) -> None:
        pass


class BasicAuthProvider(BaseAuthProvider):
    """Basic Auth provider for SuccessFactors (username@company_id : password)."""

    def __init__(self, username: str, password: str, company_id: str) -> None:
        self.username = username
        self.password = password
        self.company_id = company_id
        self._auth_header = self._build_header()

    def _build_header(self) -> str:
        if "@" in self.username:
            full_user = self.username
        else:
            full_user = f"{self.username}@{self.company_id}"
        creds = f"{full_user}:{self.password}"
        encoded = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
        return f"Basic {encoded}"

    def get_auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }

    def invalidate(self) -> None:
        pass


class OAuth2SAMLBearerAuthProvider(BaseAuthProvider):
    """
    Implements SAP SuccessFactors OAuth 2.0 SAML 2.0 Bearer Assertion Flow.
    Generates a cryptographically signed SAML 2.0 assertion (RSA-SHA256),
    exchanges it for an OAuth Bearer token, and manages token caching/refresh.
    """

    def __init__(
        self,
        client_id: str,
        user_id: str,
        company_id: str,
        token_url: str,
        private_key_pem: str,
        token_validity_buffer_seconds: int = 120,
    ) -> None:
        self.client_id = client_id
        self.user_id = user_id
        self.company_id = company_id
        self.token_url = token_url
        self.token_validity_buffer_seconds = token_validity_buffer_seconds

        self._private_key = self._load_private_key(private_key_pem)
        self._cached_token: Optional[str] = None
        self._token_expiry_timestamp: float = 0.0

    def _load_private_key(self, pem_str: str) -> rsa.RSAPrivateKey:
        try:
            key_bytes = pem_str.strip().encode("utf-8")
            key = serialization.load_pem_private_key(
                key_bytes,
                password=None,
            )
            if not isinstance(key, rsa.RSAPrivateKey):
                raise SFAuthenticationError("Provided private key is not an RSA private key.")
            return key
        except Exception as e:
            raise SFAuthenticationError(f"Failed to load RSA private key for SAML assertion: {e}") from e

    def _generate_saml_assertion(self) -> str:
        """
        Build and sign a SAML 2.0 Assertion for SuccessFactors.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        not_before = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        not_on_or_after = (now + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        issue_instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        assertion_id = f"saml-{uuid.uuid4()}"

        # Standard SF SAML 2.0 Assertion structure
        saml_template = (
            f'<saml2:Assertion xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="{assertion_id}" IssueInstant="{issue_instant}" Version="2.0">'
            f'<saml2:Issuer>{self.client_id}</saml2:Issuer>'
            f'<saml2:Subject>'
            f'<saml2:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">{self.user_id}</saml2:NameID>'
            f'<saml2:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml2:SubjectConfirmationData NotOnOrAfter="{not_on_or_after}" Recipient="{self.token_url}"/>'
            f'</saml2:SubjectConfirmation>'
            f'</saml2:Subject>'
            f'<saml2:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">'
            f'<saml2:AudienceRestriction>'
            f'<saml2:Audience>www.successfactors.com</saml2:Audience>'
            f'</saml2:AudienceRestriction>'
            f'</saml2:Conditions>'
            f'<saml2:AuthnStatement AuthnInstant="{issue_instant}">'
            f'<saml2:AuthnContext>'
            f'<saml2:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml2:AuthnContextClassRef>'
            f'</saml2:AuthnContext>'
            f'</saml2:AuthnStatement>'
            f'</saml2:Assertion>'
        )

        # Sign assertion content
        try:
            signature = self._private_key.sign(
                saml_template.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            sig_b64 = base64.b64encode(signature).decode("utf-8")
        except Exception as e:
            raise SFAuthenticationError(f"Failed to cryptographically sign SAML assertion: {e}") from e

        # In standard SF OAuth2 token flow, the assertion string itself is base64-encoded
        encoded_assertion = base64.b64encode(saml_template.encode("utf-8")).decode("utf-8")
        return encoded_assertion

    def _fetch_access_token(self) -> str:
        """Perform OAuth2 token request to SAP SuccessFactors."""
        assertion = self._generate_saml_assertion()
        payload = {
            "client_id": self.client_id,
            "company_id": self.company_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:saml2-bearer",
            "assertion": assertion,
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        logger.debug("Requesting new OAuth2 token from: %s", self.token_url)
        try:
            response = requests.post(
                self.token_url,
                data=payload,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as e:
            raise SFAuthenticationError(f"OAuth token endpoint network failure ({self.token_url}): {e}") from e

        if response.status_code != 200:
            logger.error("Token exchange failed HTTP %s: %s", response.status_code, response.text)
            raise SFAuthenticationError(
                f"Failed to acquire OAuth token (HTTP {response.status_code}): {response.text}"
            )

        try:
            data = response.json()
            access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 3600))
            if not access_token:
                raise SFAuthenticationError(f"No access_token field in token response: {data}")

            self._cached_token = access_token
            self._token_expiry_timestamp = time.time() + expires_in - self.token_validity_buffer_seconds
            logger.info("Successfully refreshed OAuth2 Bearer token (valid for %d seconds).", expires_in)
            return access_token
        except Exception as e:
            raise SFAuthenticationError(f"Failed to parse OAuth2 token response: {e}") from e

    def get_auth_headers(self) -> dict[str, str]:
        """Return cached token or fetch new token if expired."""
        if not self._cached_token or time.time() >= self._token_expiry_timestamp:
            self._cached_token = self._fetch_access_token()

        return {
            "Authorization": f"Bearer {self._cached_token}",
            "Accept": "application/json",
        }

    def invalidate(self) -> None:
        """Force next request to renew token."""
        self._cached_token = None
        self._token_expiry_timestamp = 0.0


def create_auth_provider(config: AppConfig) -> BaseAuthProvider:
    """Factory to instantiate the appropriate Auth Provider based on configuration."""
    if config.auth_type == "mock":
        return MockAuthProvider()

    if config.auth_type == "basic":
        if not config.sf_username or not config.sf_password:
            raise SFAuthenticationError("Basic auth requires 'sf_username' and 'sf_password'.")
        return BasicAuthProvider(
            username=config.sf_username,
            password=config.sf_password,
            company_id=config.sf_company_id,
        )

    if config.auth_type == "oauth2_saml":
        if not config.sf_client_id:
            raise SFAuthenticationError("OAuth2 SAML requires 'sf_client_id'.")
        if not config.sf_user_id:
            raise SFAuthenticationError("OAuth2 SAML requires 'sf_user_id'.")

        private_key = config.sf_private_key_content
        if not private_key and config.sf_private_key_path:
            try:
                with open(config.sf_private_key_path, "r", encoding="utf-8") as f:
                    private_key = f.read()
            except Exception as e:
                raise SFAuthenticationError(f"Cannot read private key file '{config.sf_private_key_path}': {e}") from e

        if not private_key:
            raise SFAuthenticationError("OAuth2 SAML requires 'sf_private_key_path' or 'sf_private_key_content'.")

        return OAuth2SAMLBearerAuthProvider(
            client_id=config.sf_client_id,
            user_id=config.sf_user_id,
            company_id=config.sf_company_id,
            token_url=config.get_token_url(),
            private_key_pem=private_key,
        )

    raise SFAuthenticationError(f"Unknown auth_type: '{config.auth_type}'.")
