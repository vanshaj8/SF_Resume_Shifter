"""Core business logic package for SAP SuccessFactors Resume Shifter."""
from core.models import (
    ApplicationStatus,
    BatchRunSummary,
    CandidateResumeData,
    JobApplicationData,
    ProcessResult,
    RunStatus,
)
from core.watermark import WatermarkManager

__all__ = [
    "WatermarkManager",
    "ApplicationStatus",
    "RunStatus",
    "CandidateResumeData",
    "JobApplicationData",
    "ProcessResult",
    "BatchRunSummary",
]
