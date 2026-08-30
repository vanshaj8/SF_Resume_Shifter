"""
Integration and Unit tests for BatchEngine: pagination, discovery,
watermark preservation on ERRORED, and advance on COMPLETED.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import pytest

from config.settings import AppConfig
from core.engine import BatchEngine
from core.models import ApplicationStatus, RunStatus
from core.processor import ApplicationProcessor
from core.watermark import WatermarkManager
from tests.mock_sf_server import MockSFDatabase, MockSFODataClient


@pytest.fixture
def temp_env(tmp_path):
    wm_file = tmp_path / "watermark.txt"
    logs_dir = tmp_path / "logs"
    summary_file = tmp_path / "summary.csv"
    cfg = AppConfig(
        watermark_file_path=wm_file,
        logs_dir=logs_dir,
        summary_csv_path=summary_file,
        batch_top=2,  # Set small page size to test pagination loop
        rate_limit_pause_seconds=0.0,
    )
    db = MockSFDatabase()
    client = MockSFODataClient(config=cfg, db=db)
    wm_mgr = WatermarkManager(wm_file)
    processor = ApplicationProcessor(client)
    engine = BatchEngine(
        config=cfg,
        client=client,
        watermark_manager=wm_mgr,
        processor=processor,
    )
    return {
        "cfg": cfg,
        "db": db,
        "client": client,
        "wm_mgr": wm_mgr,
        "engine": engine,
        "tmp_path": tmp_path,
    }


def test_batch_discovery_pagination_multiple_pages(temp_env):
    """Verifies that discover_applications iterates across multiple $top/$skip pages."""
    engine = temp_env["engine"]
    # Total seeded applications = 4, batch_top = 2 -> 2 full pages
    apps = list(engine.discover_applications())
    assert len(apps) == 4
    app_ids = [a.application_id for a in apps]
    assert "1001" in app_ids
    assert "1002" in app_ids
    assert "1003" in app_ids
    assert "1004" in app_ids


def test_batch_run_full_lifecycle(temp_env):
    """
    Executes a complete batch run and verifies:
    - Succeeded, skipped already set, skipped no resume counts.
    - Watermark advances to run timestamp on COMPLETED.
    """
    engine = temp_env["engine"]
    wm_mgr = temp_env["wm_mgr"]

    # Initial state has no watermark
    assert wm_mgr.get_watermark() is None

    summary = engine.run()
    assert summary.run_status == RunStatus.COMPLETED
    assert summary.applications_found == 4
    assert summary.succeeded == 2  # 1001 and 1004
    assert summary.skipped_already_set == 1  # 1002
    assert summary.skipped_no_resume == 1  # 1003
    assert summary.failed == 0

    # Watermark should now exist and be updated
    new_wm = wm_mgr.get_watermark()
    assert new_wm is not None
    assert new_wm.strftime("%Y-%m-%dT%H:%M:%SZ") == summary.run_timestamp


def test_watermark_preservation_on_errored_batch(temp_env, monkeypatch):
    """
    If a fatal batch error occurs during execution:
    - RunStatus must be ERRORED.
    - Watermark must be preserved (not updated) for replay capability.
    """
    engine = temp_env["engine"]
    wm_mgr = temp_env["wm_mgr"]

    # Pre-seed initial watermark
    initial_wm = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
    wm_mgr.save_watermark(initial_wm)

    # Monkeypatch discover_applications to simulate a fatal connection error
    def broken_discover(*args, **kwargs):
        raise ConnectionResetError("SF OData Gateway Connection Reset")

    monkeypatch.setattr(engine.client, "discover_applications_page", broken_discover)

    summary = engine.run()
    assert summary.run_status == RunStatus.ERRORED
    assert summary.failed == 1

    # Watermark must remain the initial watermark (Replay Safe)
    preserved_wm = wm_mgr.get_watermark()
    assert preserved_wm == initial_wm


def test_single_application_mode_execution(temp_env):
    """Tests single application execution via CLI helper."""
    engine = temp_env["engine"]
    result = engine.run_single("1001")
    assert result.status == ApplicationStatus.SUCCESS
    assert result.application_id == "1001"
    assert result.attachment_present is True


def test_concurrent_batch_run_execution(temp_env):
    """Verifies that multi-threaded batch run (concurrency=4) correctly processes all records."""
    engine = temp_env["engine"]
    summary = engine.run(concurrency=4)
    assert summary.run_status == RunStatus.COMPLETED
    assert summary.applications_found == 4
    assert summary.succeeded == 2
    assert summary.skipped_already_set == 1
    assert summary.skipped_no_resume == 1
    assert summary.failed == 0
    assert summary.elapsed_seconds >= 0.0
    assert summary.throughput_records_per_sec >= 0.0


def test_batch_run_with_skip_verification(temp_env):
    """Verifies batch run executes successfully when verify=False is specified."""
    engine = temp_env["engine"]
    summary = engine.run(verify=False)
    assert summary.run_status == RunStatus.COMPLETED
    assert summary.succeeded == 2


def test_watermark_preservation_on_individual_application_failure(temp_env, monkeypatch):
    """
    Verifies that if an individual application processing fails:
    - Watermark is preserved at the previous state (NOT advanced).
    - Overall run_status is marked ERRORED so next run retries.
    """
    engine = temp_env["engine"]
    wm_mgr = temp_env["wm_mgr"]

    initial_wm = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
    wm_mgr.save_watermark(initial_wm)

    # Monkeypatch process_application to fail on application 1001
    orig_process = engine.process_application

    def fail_1001(application, run_timestamp_str, **kwargs):
        if application.application_id == "1001":
            return ProcessResult(
                run_timestamp=run_timestamp_str,
                application_id=application.application_id,
                candidate_id=application.candidate_id,
                status=ApplicationStatus.FAILED,
                error_message="Simulated processing exception",
            )
        return orig_process(application, run_timestamp_str, **kwargs)

    monkeypatch.setattr(engine, "process_application", fail_1001)

    summary = engine.run()
    assert summary.failed == 1
    assert summary.run_status == RunStatus.ERRORED

    # Watermark must remain preserved at initial_wm
    assert wm_mgr.get_watermark() == initial_wm


def test_intermediate_application_captured_in_next_run(temp_env):
    """
    Verifies that an application created DURING Run 1 (after discovery query fires)
    is still discovered in Run 2 because Run 1's watermark was saved as run_start_time.
    """
    engine = temp_env["engine"]
    db = temp_env["db"]
    wm_mgr = temp_env["wm_mgr"]

    # Initial run processes all 4 pre-seeded applications
    summary_1 = engine.run()
    assert summary_1.run_status == RunStatus.COMPLETED
    assert summary_1.applications_found == 4

    saved_wm_1 = wm_mgr.get_watermark()
    assert saved_wm_1 is not None

    # Simulate new application 2001 created 1 second after Run 1's start_time
    app_date_dt = datetime.fromtimestamp(saved_wm_1.timestamp() + 1.0, tz=timezone.utc)
    sample_b64 = base64.b64encode(b"%PDF-1.4 New Resume Content").decode("utf-8")
    db.candidates["9901"] = {
        "candidateId": "9901",
        "firstName": "New",
        "lastName": "Applicant",
        "resume": {
            "attachmentId": "20001",
            "fileName": "new_resume.pdf",
            "fileContent": sample_b64,
            "module": "RECRUITING",
        },
    }
    db.job_applications["2001"] = {
        "applicationId": "2001",
        "candidateId": "9901",
        "applicationDate": app_date_dt,
        "Cust_Candidate_Resume": None,
    }

    # Run 2 starts now and uses saved_wm_1
    summary_2 = engine.run()
    assert summary_2.run_status == RunStatus.COMPLETED
    assert summary_2.applications_found == 1
    assert summary_2.succeeded == 1


