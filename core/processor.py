"""
Application Processor Module.
Executes the strict 7-step pipeline for an individual JobApplication record,
enforcing WRITE-ONCE business rules and verification.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from client.exceptions import (
    SFIntegrationError,
    SFODataError,
    SFPayloadValidationError,
    SFVerificationError,
    SFWriteOnceViolationError,
)
from client.odata_client import SFODataClient
from core.models import (
    ApplicationStatus,
    JobApplicationData,
    ProcessResult,
)

logger = logging.getLogger("ResumeShifter.Processor")


class ApplicationProcessor:
    """
    Encapsulates the end-to-end processing pipeline for a single Job Application.
    Guarantees that Cust_Candidate_Resume is treated as WRITE-ONCE and frozen forever.
    """

    def __init__(self, client: SFODataClient, default_verify: bool = True) -> None:
        self.client = client
        self.default_verify = default_verify

    def process_application(
        self,
        application: JobApplicationData,
        run_timestamp: Optional[str] = None,
        dry_run: bool = False,
        force: bool = False,
        verify: Optional[bool] = None,
    ) -> ProcessResult:
        """
        Executes the 7-step single application processing flow:

        Step 1: Check JobApplication.Cust_Candidate_Resume -> SKIPPED_ALREADY_SET if populated (unless force=True).
        Step 2: Retrieve Candidate resume (GET Candidate with $expand=resume, with LRU cache).
        Step 3: Validate resume existence, fileContent, and fileName -> SKIPPED_NO_RESUME if missing.
        Step 4: Build copy payload preserving exact original candidate fileName and base64 fileContent.
        Step 5: Update Job Application via POST /odata/v2/upsert.
        Step 6: Verify upsert via GET JobApplication with $expand=Cust_Candidate_Resume (optional/configurable).
        Step 7: Mark result as SUCCESS or FAILED.
        """
        ts = run_timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        app_id = application.application_id
        cand_id = application.candidate_id
        should_verify = self.default_verify if verify is None else verify

        logger.info(
            "--- Processing Application ID: %s | Candidate ID: %s ---",
            app_id,
            cand_id,
        )

        # -------------------------------------------------------------------------
        # Step 1: Check JobApplication.Cust_Candidate_Resume (WRITE-ONCE Check)
        # -------------------------------------------------------------------------
        if application.is_custom_resume_populated and not force:
            logger.info(
                "[Step 1] WRITE-ONCE Enforced: JobApplication %s already has Cust_Candidate_Resume populated "
                "(Attachment ID: %s, File: '%s'). Skipping copy to preserve frozen historical snapshot.",
                app_id,
                application.custom_resume_attachment_id,
                application.custom_resume_file_name,
            )
            return ProcessResult(
                run_timestamp=ts,
                application_id=app_id,
                candidate_id=cand_id,
                status=ApplicationStatus.SKIPPED_ALREADY_SET,
                attachment_present=True,
                error_message="",
            )

        if force and application.is_custom_resume_populated:
            logger.warning("[Step 1] FORCE flag enabled: Bypassing WRITE-ONCE check for application %s.", app_id)
        else:
            logger.debug("[Step 1] Cust_Candidate_Resume is empty on application %s. Proceeding to fetch candidate resume.", app_id)

        try:
            # ---------------------------------------------------------------------
            # Step 2: Retrieve Candidate resume
            # ---------------------------------------------------------------------
            if not cand_id:
                raise SFPayloadValidationError(f"Application {app_id} does not have an associated candidateId.")

            logger.debug("[Step 2] Retrieving resume for Candidate %s", cand_id)
            candidate_resume = self.client.get_candidate_resume(cand_id)

            # ---------------------------------------------------------------------
            # Step 3: Validate Candidate resume existence & contents
            # ---------------------------------------------------------------------
            if not candidate_resume.has_valid_resume:
                logger.info(
                    "[Step 3] Candidate %s profile has no active resume or fileContent is empty. "
                    "Skipping application %s.",
                    cand_id,
                    app_id,
                )
                return ProcessResult(
                    run_timestamp=ts,
                    application_id=app_id,
                    candidate_id=cand_id,
                    status=ApplicationStatus.SKIPPED_NO_RESUME,
                    attachment_present=False,
                    error_message="Candidate profile does not contain a valid resume attachment.",
                )

            logger.info(
                "[Step 3] Found valid candidate resume: '%s' (module=%s, size_b64=%d bytes).",
                candidate_resume.file_name,
                candidate_resume.module,
                len(candidate_resume.file_content_b64 or ""),
            )

            # ---------------------------------------------------------------------
            # Step 4: Build copy payload preserving exact original candidate fileName
            # ---------------------------------------------------------------------
            file_name = candidate_resume.file_name or "resume.pdf"
            file_content = candidate_resume.file_content_b64 or ""
            module = candidate_resume.module or "RECRUITING"

            logger.debug(
                "[Step 4] Built SFOData.JobApplication payload preserving original filename '%s' and nested Cust_Candidate_Resume attachment.",
                file_name,
            )

            if dry_run:
                logger.info(
                    "[DRY RUN] [Step 5 & 6 Skipped] Would upsert '%s' into JobApplication %s. Cust_Candidate_Resume.",
                    file_name,
                    app_id,
                )
                return ProcessResult(
                    run_timestamp=ts,
                    application_id=app_id,
                    candidate_id=cand_id,
                    status=ApplicationStatus.SUCCESS,
                    attachment_present=True,
                    error_message="DRY_RUN_SIMULATION",
                )

            # ---------------------------------------------------------------------
            # Step 5: Update Job Application (POST /odata/v2/upsert)
            # ---------------------------------------------------------------------
            logger.info(
                "[Step 5] Upserting resume '%s' directly into JobApplication %s Cust_Candidate_Resume...",
                file_name,
                app_id,
            )
            self.client.upsert_job_application_resume(
                application_id=app_id,
                file_name=file_name,
                file_content_b64=file_content,
                module=module,
            )

            # ---------------------------------------------------------------------
            # Step 6: Verify Job Application (Conditional)
            # ---------------------------------------------------------------------
            if should_verify:
                logger.info("[Step 6] Verifying JobApplication %s Cust_Candidate_Resume persistence...", app_id)
                verified_app = self.client.verify_job_application_resume(
                    application_id=app_id,
                    expected_file_name=file_name,
                )
                logger.info(
                    "[Step 6] Verification passed for application %s (Attachment ID: %s, File: '%s').",
                    app_id,
                    verified_app.custom_resume_attachment_id,
                    verified_app.custom_resume_file_name,
                )
            else:
                logger.debug("[Step 6] Post-upsert GET verification skipped (verify=False).")

            # ---------------------------------------------------------------------
            # Step 7: Mark Result SUCCESS
            # ---------------------------------------------------------------------
            logger.info("[Step 7] Result: SUCCESS for Application %s", app_id)
            return ProcessResult(
                run_timestamp=ts,
                application_id=app_id,
                candidate_id=cand_id,
                status=ApplicationStatus.SUCCESS,
                attachment_present=True,
                error_message="",
            )

        except (SFODataError, SFPayloadValidationError, SFVerificationError, SFIntegrationError) as exc:
            error_text = f"Integration error: {exc}"
            logger.error("[Step 7] Result: FAILED for Application %s. Reason: %s", app_id, error_text)
            return ProcessResult(
                run_timestamp=ts,
                application_id=app_id,
                candidate_id=cand_id,
                status=ApplicationStatus.FAILED,
                attachment_present=False,
                error_message=error_text,
            )
        except Exception as exc:
            error_text = f"Unexpected error: {exc}"
            logger.exception("[Step 7] Result: FAILED with unexpected exception for Application %s: %s", app_id, exc)
            return ProcessResult(
                run_timestamp=ts,
                application_id=app_id,
                candidate_id=cand_id,
                status=ApplicationStatus.FAILED,
                attachment_present=False,
                error_message=error_text,
            )
