"""
Data models and Enums for SAP SuccessFactors Recruiting Resume Shifter.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    """Execution status for an individual job application."""
    SUCCESS = "SUCCESS"
    SKIPPED_ALREADY_SET = "SKIPPED_ALREADY_SET"
    SKIPPED_NO_RESUME = "SKIPPED_NO_RESUME"
    FAILED = "FAILED"


class RunStatus(str, Enum):
    """Execution status for the entire batch run."""
    COMPLETED = "COMPLETED"
    ERRORED = "ERRORED"


class CandidateResumeData(BaseModel):
    """Encapsulates Candidate Profile resume attachment fields."""
    candidate_id: str
    attachment_id: Optional[str] = None
    file_name: Optional[str] = None
    file_content_b64: Optional[str] = None
    module: str = "RECRUITING"

    @property
    def has_valid_resume(self) -> bool:
        """Returns True if resume exists with non-empty fileName and fileContent."""
        return bool(self.file_name and self.file_content_b64)


class JobApplicationData(BaseModel):
    """Encapsulates Job Application entity and target custom attachment field state."""
    application_id: str
    candidate_id: str
    application_date: Optional[datetime] = None
    custom_resume_attachment_id: Optional[str] = None
    custom_resume_file_name: Optional[str] = None
    custom_resume_raw: Optional[dict[str, Any]] = None

    @property
    def is_custom_resume_populated(self) -> bool:
        """
        Determines whether Cust_Candidate_Resume is already populated (WRITE-ONCE check).
        Returns True if attachmentId exists (>0), or if fileName is non-empty, or if
        an attachment object is populated.
        """
        # Fast-path 1: Attachment ID is present
        att_id = self.custom_resume_attachment_id
        if att_id:
            try:
                # In SF OData, attachmentId = -1, 0, or null denotes empty
                if int(att_id) > 0:
                    return True
            except (ValueError, TypeError):
                if att_id.strip() and att_id.lower() not in ("none", "null", "0", "-1"):
                    return True

        # Fast-path 2: File name is present
        fn = self.custom_resume_file_name
        if fn and fn.strip():
            return True

        # Fast-path 3: Inspect raw JSON payload structure if present
        raw = self.custom_resume_raw
        if not raw or not isinstance(raw, dict):
            return False

        # Check if wrapped in results list
        results_list = raw.get("results")
        if isinstance(results_list, list):
            if not results_list:
                return False
            first_att = results_list[0]
            if isinstance(first_att, dict):
                raw_id = first_att.get("attachmentId")
                if raw_id:
                    try:
                        if int(raw_id) > 0:
                            return True
                    except (ValueError, TypeError):
                        return True
                raw_fn = first_att.get("fileName")
                if raw_fn and str(raw_fn).strip():
                    return True
            return False

        # Direct dictionary check
        raw_id = raw.get("attachmentId")
        if raw_id:
            try:
                if int(raw_id) > 0:
                    return True
            except (ValueError, TypeError):
                return True
        raw_fn = raw.get("fileName")
        if raw_fn and str(raw_fn).strip():
            return True

        return False


class ProcessResult(BaseModel):
    """Result of processing a single Job Application record."""
    run_timestamp: str
    application_id: str
    candidate_id: str
    status: ApplicationStatus
    attachment_present: bool = False
    error_message: str = ""

    def to_csv_dict(self) -> dict[str, str]:
        """Convert to dictionary matching per-run CSV specification."""
        clean_err = self.error_message.replace("\n", " ").strip() if self.error_message else ""
        return {
            "runTimestamp": self.run_timestamp,
            "applicationId": self.application_id,
            "candidateId": self.candidate_id,
            "status": self.status.value,
            "attachmentPresent": "true" if self.attachment_present else "false",
            "errorMessage": clean_err,
        }


class BatchRunSummary(BaseModel):
    """Cumulative summary of a batch execution run including timing metrics."""
    run_timestamp: str
    applications_found: int = 0
    succeeded: int = 0
    skipped_already_set: int = 0
    skipped_no_resume: int = 0
    failed: int = 0
    run_status: RunStatus = RunStatus.COMPLETED
    elapsed_seconds: float = 0.0

    @property
    def throughput_records_per_sec(self) -> float:
        """Calculate throughput in records per second."""
        if self.elapsed_seconds > 0 and self.applications_found > 0:
            return round(self.applications_found / self.elapsed_seconds, 2)
        return 0.0

    def to_csv_dict(self) -> dict[str, str]:
        """Convert to dictionary matching run summary CSV specification."""
        return {
            "runTimestamp": self.run_timestamp,
            "applicationsFound": str(self.applications_found),
            "succeeded": str(self.succeeded),
            "skippedAlreadySet": str(self.skipped_already_set),
            "skippedNoResume": str(self.skipped_no_resume),
            "failed": str(self.failed),
            "runStatus": self.run_status.value,
        }
