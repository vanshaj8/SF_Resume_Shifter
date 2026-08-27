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

