"""
Unit tests for CSV Loggers (Run CSV and Summary CSV).
"""

from __future__ import annotations

import csv
from pathlib import Path
import pytest

from core.models import (
    ApplicationStatus,
    BatchRunSummary,
    ProcessResult,
    RunStatus,
)
from logging_utils.csv_logger import RunCSVLogger, SummaryCSVLogger


def test_run_csv_logger(tmp_path):
    logs_dir = tmp_path / "logs"
    ts = "2026-08-25T12:00:00Z"
    logger = RunCSVLogger(logs_dir=logs_dir, timestamp_str=ts)

    res1 = ProcessResult(
        run_timestamp=ts,
        application_id="1001",
        candidate_id="501",
        status=ApplicationStatus.SUCCESS,
        attachment_present=True,
        error_message="",
    )
    res2 = ProcessResult(
        run_timestamp=ts,
        application_id="1002",
        candidate_id="502",
        status=ApplicationStatus.SKIPPED_ALREADY_SET,
        attachment_present=True,
        error_message="",
    )

    logger.write_row(res1)
    logger.write_row(res2)

    assert logger.file_path.exists()

    with open(logger.file_path, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2

        assert reader[0]["runTimestamp"] == ts
        assert reader[0]["applicationId"] == "1001"
        assert reader[0]["candidateId"] == "501"
        assert reader[0]["status"] == "SUCCESS"
        assert reader[0]["attachmentPresent"] == "true"
        assert reader[0]["errorMessage"] == ""

        assert reader[1]["applicationId"] == "1002"
        assert reader[1]["status"] == "SKIPPED_ALREADY_SET"


def test_summary_csv_logger(tmp_path):
    summary_path = tmp_path / "resume_snapshot_run_summary.csv"
    summary_logger = SummaryCSVLogger(summary_path)

    sum1 = BatchRunSummary(
        run_timestamp="2026-08-25T12:00:00Z",
        applications_found=10,
        succeeded=8,
        skipped_already_set=1,
        skipped_no_resume=1,
        failed=0,
        run_status=RunStatus.COMPLETED,
    )
    summary_logger.record_summary(sum1)

    with open(summary_path, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["runTimestamp"] == "2026-08-25T12:00:00Z"
        assert reader[0]["applicationsFound"] == "10"
        assert reader[0]["succeeded"] == "8"
        assert reader[0]["skippedAlreadySet"] == "1"
        assert reader[0]["skippedNoResume"] == "1"
        assert reader[0]["failed"] == "0"
        assert reader[0]["runStatus"] == "COMPLETED"
