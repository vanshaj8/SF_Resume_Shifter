"""
CSV Logging Managers for per-run logs and cumulative summary tracking.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import List, Optional

from core.models import BatchRunSummary, ProcessResult

logger = logging.getLogger("ResumeShifter.CSVLogger")


class RunCSVLogger:
    """
    Manages per-run CSV log writing:
    File: logs/resume_snapshot_log_<timestamp>.csv
    Columns: runTimestamp, applicationId, candidateId, status, attachmentPresent, errorMessage
    """

    HEADERS = [
        "runTimestamp",
        "applicationId",
        "candidateId",
        "status",
        "attachmentPresent",
        "errorMessage",
    ]

    def __init__(self, logs_dir: Path | str, timestamp_str: str) -> None:
        self.logs_dir = Path(logs_dir).resolve()
        self.timestamp_str = timestamp_str
        # Sanitize timestamp for filename
        clean_ts = timestamp_str.replace(":", "").replace("-", "").replace("Z", "")
        self.file_path = self.logs_dir / f"resume_snapshot_log_{clean_ts}.csv"
        self._initialized = False

    def _ensure_file_initialized(self) -> None:
        if not self._initialized:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            if not self.file_path.exists():
                with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                    writer.writeheader()
            self._initialized = True

    def write_row(self, result: ProcessResult) -> None:
        """Append a single application execution result to the run CSV."""
        self._ensure_file_initialized()
        with open(self.file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS)
            writer.writerow(result.to_csv_dict())

    def write_rows(self, results: List[ProcessResult]) -> None:
        """Batch write multiple results to the run CSV."""
        self._ensure_file_initialized()
        with open(self.file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS)
            for r in results:
                writer.writerow(r.to_csv_dict())

        logger.info("Recorded %d application results in %s", len(results), self.file_path)


class SummaryCSVLogger:
    """
    Manages cumulative run summary CSV logging:
    File: resume_snapshot_run_summary.csv
    Columns: runTimestamp, applicationsFound, succeeded, skippedAlreadySet, skippedNoResume, failed, runStatus
    """

    HEADERS = [
        "runTimestamp",
        "applicationsFound",
        "succeeded",
        "skippedAlreadySet",
        "skippedNoResume",
        "failed",
        "runStatus",
    ]

    def __init__(self, summary_csv_path: Path | str = "resume_snapshot_run_summary.csv") -> None:
        self.file_path = Path(summary_csv_path).resolve()
        self._ensure_file_initialized()

    def _ensure_file_initialized(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists() or self.file_path.stat().st_size == 0:
            with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                writer.writeheader()

    def record_summary(self, summary: BatchRunSummary) -> None:
        """Append a batch run summary record to the cumulative CSV."""
        self._ensure_file_initialized()
        with open(self.file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS)
            writer.writerow(summary.to_csv_dict())

        logger.info("Recorded batch run summary in %s", self.file_path)
