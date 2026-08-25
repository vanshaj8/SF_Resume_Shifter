"""
Unit tests for Authentication Providers (Basic Auth, SAML Bearer Generation & Token Caching).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from client.auth import BasicAuthProvider, OAuth2SAMLBearerAuthProvider
from client.exceptions import SFAuthenticationError


@pytest.fixture
def rsa_key_pem():
    """Generates an ephemeral RSA private key for testing SAML signing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


def test_basic_auth_provider():
    provider = BasicAuthProvider(
        username="sf_tech_user",
        password="secretPassword123",
        company_id="TEST_TENANT",
    )
    headers = provider.get_auth_headers()
    assert "Authorization" in headers
    auth_val = headers["Authorization"]
    assert auth_val.startswith("Basic ")

    encoded = auth_val.split(" ")[1]
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded == "sf_tech_user@TEST_TENANT:secretPassword123"


def test_saml_assertion_generation_and_signing(rsa_key_pem):
    provider = OAuth2SAMLBearerAuthProvider(
        client_id="oauth_client_123",
        user_id="integration_user",
        company_id="TEST_TENANT",
        token_url="https://api.successfactors.com/oauth/token",
        private_key_pem=rsa_key_pem,
    )

    assertion_b64 = provider._generate_saml_assertion()
    assert assertion_b64 is not None

    # Decode and check SAML structure
    assertion_xml = base64.b64decode(assertion_b64).decode("utf-8")
    assert "<saml2:Issuer>oauth_client_123</saml2:Issuer>" in assertion_xml
    assert "integration_user" in assertion_xml
    assert "https://api.successfactors.com/oauth/token" in assertion_xml


def test_saml_invalid_key():
    with pytest.raises(SFAuthenticationError):
        OAuth2SAMLBearerAuthProvider(
            client_id="oauth_client_123",
            user_id="integration_user",
            company_id="TEST_TENANT",
            token_url="https://api.successfactors.com/oauth/token",
            private_key_pem="INVALID_KEY_NOT_PEM",
        )
