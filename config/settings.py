"""
Application Configuration Module for SAP SuccessFactors Recruiting Integration.
Provides strongly-typed settings using Pydantic, supporting environment variables,
.env files, and secret masking.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Configuration settings for the SAP SuccessFactors Resume Shifter integration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # SuccessFactors OData Endpoint Configuration
    sf_api_base_url: str = Field(
        default="https://api12preview.sapsf.eu/odata/v2",
        description="Base URL for SuccessFactors OData v2 API (e.g. https://api12preview.sapsf.eu/odata/v2)",
    )
    sf_company_id: str = Field(
        default="SF_CORP_DEMO",
        description="SAP SuccessFactors Company / Tenant ID",
    )

    # Authentication Configuration
    auth_type: Literal["oauth2_saml", "basic", "mock"] = Field(
        default="oauth2_saml",
        description="Authentication method: 'oauth2_saml', 'basic', or 'mock'",
    )

    # OAuth 2.0 SAML Bearer Flow Settings
    sf_client_id: Optional[str] = Field(
        default=None,
        description="OAuth2 API Key / Client ID registered in SF Security Center",
    )
    sf_user_id: Optional[str] = Field(
        default=None,
        description="Integration technical user ID with Recruiting permissions",
    )
    sf_token_url: Optional[str] = Field(
        default=None,
        description="OAuth 2.0 Token Endpoint (e.g. https://api12preview.sapsf.eu/oauth/token)",
    )
    sf_private_key_path: Optional[str] = Field(
        default=None,
        description="Path to PEM private key file for SAML assertion signing",
    )
    sf_private_key_content: Optional[str] = Field(
        default=None,
        description="Raw PEM private key content string",
    )

    # Basic Authentication Settings (Alternative / Dev)
    sf_username: Optional[str] = Field(
        default=None,
        description="SF Technical Username (without @company_id)",
    )
    sf_password: Optional[str] = Field(
        default=None,
        description="SF Technical User Password",
    )

    # Batch Engine & API Client Tuning
    batch_top: int = Field(
        default=1000,
        description="Number of records to fetch per OData $top page (max 1000)",
    )
    max_workers: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of parallel worker threads for batch application processing (1 = sequential)",
    )
    verify_upsert: bool = Field(
        default=True,
        description="Whether to perform an explicit GET request to verify Cust_Candidate_Resume post-upsert",
    )
    candidate_cache_size: int = Field(
        default=2000,
        ge=0,
        description="Maximum number of candidate profile resumes to cache in-memory during batch processing",
    )
    request_timeout_seconds: int = Field(
        default=60,
        description="HTTP request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        description="Maximum retry attempts on transient network/429/5xx errors",
    )
    backoff_factor: float = Field(
        default=1.0,
        description="Exponential backoff factor for HTTP retries",
    )
    rate_limit_pause_seconds: float = Field(
        default=0.05,
        description="Optional pause between processing individual applications (in seconds)",
    )

    # State & Logging Files
    watermark_file_path: Path = Field(
        default=Path("watermark.txt"),
        description="File path where the last processed timestamp is persisted",
    )
    logs_dir: Path = Field(
        default=Path("logs"),
        description="Directory for per-run execution CSV logs and detailed application logs",
    )
    summary_csv_path: Path = Field(
        default=Path("resume_snapshot_run_summary.csv"),
        description="Cumulative run summary CSV file path",
    )

    # Logging Configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    def get_token_url(self) -> str:
        """Derive token URL if not explicitly provided."""
        if self.sf_token_url:
            return self.sf_token_url
        base = self.sf_api_base_url.rstrip("/")
        if "/odata/v2" in base:
            base = base.replace("/odata/v2", "")
        return f"{base}/oauth/token"

    def get_api_endpoint(self, path: str) -> str:
        """Construct a full OData endpoint URL ensuring no double slashes."""
        base = self.sf_api_base_url.rstrip("/")
        clean_path = path.lstrip("/")
        return f"{base}/{clean_path}"

    def safe_dict(self) -> dict:
        """Export settings with sensitive credentials redacted for safe logging."""
        data = self.model_dump()
        redacted = "[REDACTED]"
        if data.get("sf_password"):
            data["sf_password"] = redacted
        if data.get("sf_private_key_content"):
            data["sf_private_key_content"] = redacted
        if data.get("sf_client_id"):
            data["sf_client_id"] = (
                data["sf_client_id"][:4] + "****" if len(data["sf_client_id"]) > 4 else redacted
            )
        return data


def get_config() -> AppConfig:
    """Factory to load configuration from environment or .env file."""
    return AppConfig()
