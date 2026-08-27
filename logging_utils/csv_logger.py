"""
CSV Logging Managers for per-run logs and cumulative summary tracking.
"""

from __future__ import annotations

import csv
import logging
import threading
from pathlib import Path
from typing import IO, List, Optional

from core.models import BatchRunSummary, ProcessResult

logger = logging.getLogger("ResumeShifter.CSVLogger")


class RunCSVLogger:
    """
    Thread-safe manager for per-run CSV execution logging.
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
        clean_ts = timestamp_str.replace(":", "").replace("-", "").replace("Z", "")
        self.file_path = self.logs_dir / f"resume_snapshot_log_{clean_ts}.csv"
        self._lock = threading.Lock()
        self._file_handle: Optional[IO[str]] = None
        self._writer: Optional[csv.DictWriter] = None
        self._initialized = False

    def _ensure_file_initialized(self) -> None:
        if not self._initialized:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            if not self.file_path.exists():
                with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                    writer.writeheader()
            self._initialized = True

    def __enter__(self) -> "RunCSVLogger":
        """Context manager allowing continuous open handle for maximum I/O throughput."""
        self._ensure_file_initialized()
        self._file_handle = open(self.file_path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file_handle, fieldnames=self.HEADERS)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close open file handle on context exit."""
        self.close()

    def close(self) -> None:
        """Flush and close active file handle if open."""
        with self._lock:
            if self._file_handle and not self._file_handle.closed:
                try:
                    self._file_handle.flush()
                    self._file_handle.close()
                except Exception:
                    pass
                self._file_handle = None
                self._writer = None

    def write_row(self, result: ProcessResult) -> None:
        """Thread-safe append of a single application execution result to the run CSV."""
        with self._lock:
            if self._file_handle and not self._file_handle.closed and self._writer:
                self._writer.writerow(result.to_csv_dict())
                self._file_handle.flush()
            else:
                self._ensure_file_initialized()
                with open(self.file_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                    writer.writerow(result.to_csv_dict())

    def write_rows(self, results: List[ProcessResult]) -> None:
        """Thread-safe batch write of multiple results to the run CSV."""
        if not results:
            return
        with self._lock:
            if self._file_handle and not self._file_handle.closed and self._writer:
                for r in results:
                    self._writer.writerow(r.to_csv_dict())
                self._file_handle.flush()
            else:
                self._ensure_file_initialized()
                with open(self.file_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                    for r in results:
                        writer.writerow(r.to_csv_dict())

        logger.info("Recorded %d application results in %s", len(results), self.file_path)


class SummaryCSVLogger:
    """
    Thread-safe manager for cumulative run summary CSV logging.
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
        self._lock = threading.Lock()
        self._ensure_file_initialized()

    def _ensure_file_initialized(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists() or self.file_path.stat().st_size == 0:
            with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                writer.writeheader()

    def record_summary(self, summary: BatchRunSummary) -> None:
        """Thread-safe append of a batch run summary record to the cumulative CSV."""
        with self._lock:
            self._ensure_file_initialized()
            with open(self.file_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                writer.writerow(summary.to_csv_dict())

        logger.info("Recorded batch run summary in %s", self.file_path)
