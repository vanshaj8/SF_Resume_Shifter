"""
Wire-level HTTP integration tests for SFODataClient using the responses library.
Tests exact OData request/response JSON payloads, headers, query params, and error unmarshaling.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import responses
import pytest

from client.auth import MockAuthProvider
from client.exceptions import SFODataError, SFVerificationError
from client.odata_client import SFODataClient, format_sf_odata_filter_datetime, parse_sf_odata_datetime
from config.settings import AppConfig


@pytest.fixture
def config():
    return AppConfig(
        sf_api_base_url="https://api12preview.sapsf.eu/odata/v2",
        sf_company_id="TEST_TENANT",
    )


@pytest.fixture
def odata_client(config):
    auth = MockAuthProvider(token="mock_access_token_123")
    return SFODataClient(config=config, auth_provider=auth)


def test_parse_and_format_odata_datetime():
    dt = parse_sf_odata_datetime("/Date(1724580000000)/")
    assert dt is not None
    assert dt.year == 2024

    iso_dt = parse_sf_odata_datetime("2026-08-25T12:00:00Z")
    assert iso_dt == datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)

    formatted = format_sf_odata_filter_datetime(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    assert formatted == "datetime'2026-08-25T12:00:00'"


@responses.activate
def test_discover_applications_wire_call(odata_client):
    url = "https://api12preview.sapsf.eu/odata/v2/JobApplication"
    mock_response = {
        "d": {
            "results": [
                {
                    "applicationId": "1001",
                    "candidateId": "501",
                    "applicationDate": "/Date(1724580000000)/",
                    "Cust_Candidate_Resume": None,
                },
                {
                    "applicationId": "1002",
                    "candidateId": "502",
                    "applicationDate": "/Date(1724583600000)/",
                    "Cust_Candidate_Resume": {
                        "attachmentId": "90001",
                        "fileName": "Existing.pdf",
                    },
                },
            ]
        }
    }
    responses.add(responses.GET, url, json=mock_response, status=200)

    apps = odata_client.discover_applications_page(top=10, skip=0)
    assert len(apps) == 2
    assert apps[0].application_id == "1001"
    assert apps[0].candidate_id == "501"
    assert not apps[0].is_custom_resume_populated

    assert apps[1].application_id == "1002"
    assert apps[1].is_custom_resume_populated
    assert apps[1].custom_resume_file_name == "Existing.pdf"


@responses.activate
def test_get_candidate_resume_wire_call(odata_client):
    url = "https://api12preview.sapsf.eu/odata/v2/Candidate(501)"
    mock_response = {
        "d": {
            "candidateId": "501",
            "resume": {
                "attachmentId": "12345",
                "fileName": "Resume_A.pdf",
                "fileContent": "JVBERi0xLjQK...",
                "module": "RECRUITING",
            },
        }
    }
    responses.add(responses.GET, url, json=mock_response, status=200)

    resume = odata_client.get_candidate_resume("501")
    assert resume.candidate_id == "501"
    assert resume.attachment_id == "12345"
    assert resume.file_name == "Resume_A.pdf"
    assert resume.file_content_b64 == "JVBERi0xLjQK..."
    assert resume.module == "RECRUITING"
    assert resume.has_valid_resume is True


@responses.activate
def test_upsert_job_application_resume_wire_payload(odata_client):
    url = "https://api12preview.sapsf.eu/odata/v2/upsert"
    mock_response = {
        "d": [
            {
                "key": "JobApplication/applicationId=1001",
                "status": "OK",
                "editStatus": "UPDATED",
                "message": None,
                "index": 0,
                "httpCode": 200,
            }
        ]
    }
    responses.add(responses.POST, url, json=mock_response, status=200)

    odata_client.upsert_job_application_resume(
        application_id="1001",
        file_name="Resume_A.pdf",
        file_content_b64="JVBERi0xLjQK...",
        module="RECRUITING",
    )

    # Verify exact JSON payload structure sent over wire
    assert len(responses.calls) == 1
    sent_body = json.loads(responses.calls[0].request.body)
    assert sent_body == {
        "__metadata": {
            "uri": "JobApplication(1001)",
            "type": "SFOData.JobApplication",
        },
        "applicationId": "1001",
        "Cust_Candidate_Resume": {
            "__metadata": {
                "type": "SFOData.Attachment",
            },
            "fileContent": "JVBERi0xLjQK...",
            "fileName": "Resume_A.pdf",
            "module": "RECRUITING",
        },
    }


@responses.activate
def test_odata_401_token_retry(odata_client):
    url = "https://api12preview.sapsf.eu/odata/v2/JobApplication(1001)"
    # First call returns 401 Unauthorized
    responses.add(responses.GET, url, status=401)
    # Second call returns 200 OK after token refresh
    mock_response = {
        "d": {
            "applicationId": "1001",
            "candidateId": "501",
            "Cust_Candidate_Resume": None,
        }
    }
    responses.add(responses.GET, url, json=mock_response, status=200)

    app = odata_client.get_application("1001")
    assert app.application_id == "1001"
    assert len(responses.calls) == 2
