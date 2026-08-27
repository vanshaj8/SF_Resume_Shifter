"""
Unit tests for ApplicationProcessor: Write-Once enforcement, Candidate resume fetching,
payload creation, upsert execution, and post-upsert verification.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import pytest

from config.settings import AppConfig
from core.models import ApplicationStatus
from core.processor import ApplicationProcessor
from tests.mock_sf_server import MockSFDatabase, MockSFODataClient


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(
        watermark_file_path=tmp_path / "watermark.txt",
        logs_dir=tmp_path / "logs",
        summary_csv_path=tmp_path / "summary.csv",
    )
    return cfg


@pytest.fixture
def mock_db():
    return MockSFDatabase()


@pytest.fixture
def client(config, mock_db):
    return MockSFODataClient(config=config, db=mock_db)


@pytest.fixture
def processor(client):
    return ApplicationProcessor(client)


def test_write_once_day1_and_day10_scenario(processor, client, mock_db):
    """
    Validates the exact Business Rule:
    Day 1:
      Candidate Resume = Resume_A.pdf
      Application 1001 = empty
      System copies Resume_A.pdf -> Application 1001 = Resume_A.pdf
    Day 10:
      Candidate updates profile resume to Resume_B.pdf
      Application 1001 must remain Resume_A.pdf forever.
    """
    # ------------------ Day 1 ------------------
    app_1001_initial = client.get_application("1001")
    assert not app_1001_initial.is_custom_resume_populated

    result_day1 = processor.process_application(app_1001_initial)
    assert result_day1.status == ApplicationStatus.SUCCESS
    assert result_day1.attachment_present is True
    assert result_day1.error_message == ""

    # Verify Day 1 attachment in DB
    app_1001_after_day1 = client.get_application("1001")
    assert app_1001_after_day1.is_custom_resume_populated
    assert app_1001_after_day1.custom_resume_file_name == "Resume_A.pdf"

    # ------------------ Day 10 ------------------
    # Candidate updates profile resume to Resume_B.pdf
    sample_b64_b = base64.b64encode(b"%PDF-1.4 Mock Resume B (Updated)").decode("utf-8")
    mock_db.candidates["501"]["resume"] = {
        "attachmentId": "10099",
        "fileName": "Resume_B.pdf",
        "fileContent": sample_b64_b,
        "module": "RECRUITING",
    }

    # Run processing again on Application 1001
    app_1001_day10 = client.get_application("1001")
    result_day10 = processor.process_application(app_1001_day10)

    # Must be SKIPPED_ALREADY_SET
    assert result_day10.status == ApplicationStatus.SKIPPED_ALREADY_SET
    assert result_day10.attachment_present is True

    # Application 1001 MUST STILL be Resume_A.pdf (Frozen Historical Snapshot)
    app_1001_final = client.get_application("1001")
    assert app_1001_final.custom_resume_file_name == "Resume_A.pdf"


def test_skipped_already_set_preexisting_snapshot(processor, client):
    """If Cust_Candidate_Resume is already populated, return SKIPPED_ALREADY_SET."""
    app_1002 = client.get_application("1002")
    assert app_1002.is_custom_resume_populated

    result = processor.process_application(app_1002)
    assert result.status == ApplicationStatus.SKIPPED_ALREADY_SET
    assert result.attachment_present is True


def test_skipped_no_resume_candidate(processor, client):
    """If candidate profile does not have a resume, return SKIPPED_NO_RESUME."""
    app_1003 = client.get_application("1003")
    assert not app_1003.is_custom_resume_populated

    result = processor.process_application(app_1003)
    assert result.status == ApplicationStatus.SKIPPED_NO_RESUME
    assert result.attachment_present is False
    assert "does not contain a valid resume" in result.error_message


def test_skipped_empty_file_content_candidate(processor, client, mock_db):
    """If candidate resume exists but fileContent is empty string, return SKIPPED_NO_RESUME."""
    mock_db.job_applications["1005"] = {
        "applicationId": "1005",
        "candidateId": "504",
        "applicationDate": datetime(2026, 8, 21, 10, 0, 0, tzinfo=timezone.utc),
        "Cust_Candidate_Resume": None,
    }
    app_1005 = client.get_application("1005")

    result = processor.process_application(app_1005)
    assert result.status == ApplicationStatus.SKIPPED_NO_RESUME
    assert result.attachment_present is False


def test_upsert_failure_marks_failed(processor, client):
    """When OData upsert encounters an error, record FAILED status."""
    app_1004 = client.get_application("1004")
    client.fail_next_upsert = True

    result = processor.process_application(app_1004)
    assert result.status == ApplicationStatus.FAILED
    assert "Simulated OData Upsert Failure" in result.error_message


def test_verification_failure_marks_failed(processor, client):
    """When post-upsert verification fails, record FAILED status."""
    app_1004 = client.get_application("1004")
    client.fail_next_verification = True

    result = processor.process_application(app_1004)
    assert result.status == ApplicationStatus.FAILED
    assert "Simulated Verification Mismatch" in result.error_message


def test_dry_run_mode(processor, client, mock_db):
    """Dry run mode should simulate success without modifying the database."""
    app_1004 = client.get_application("1004")
    assert not app_1004.is_custom_resume_populated

    result = processor.process_application(app_1004, dry_run=True)
    assert result.status == ApplicationStatus.SUCCESS
    assert result.error_message == "DRY_RUN_SIMULATION"

    # Verify DB was NOT modified
    app_1004_db = client.get_application("1004")
    assert not app_1004_db.is_custom_resume_populated


def test_candidate_resume_cache_hit(client):
    """Verify that candidate profile resume is cached in memory for repeat queries."""
    # First fetch populates cache
    res1 = client.get_candidate_resume("501")
    assert res1.file_name == "Resume_A.pdf"
    assert len(client.candidate_cache) == 1

    # Second fetch returns from cache
    res2 = client.get_candidate_resume("501")
    assert res2.file_name == "Resume_A.pdf"
    assert res1 is res2


def test_process_application_with_skip_verification(processor, client):
    """Verify that verify=False bypasses Step 6 verification even if verification would fail."""
    app_1004 = client.get_application("1004")
    # Simulate a verification mismatch that would fail Step 6 if called
    client.fail_next_verification = True

    # With verify=False, it should succeed without calling verification
    result = processor.process_application(app_1004, verify=False)
    assert result.status == ApplicationStatus.SUCCESS

