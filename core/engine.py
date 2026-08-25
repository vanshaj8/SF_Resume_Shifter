"""
Batch Orchestration Engine for SAP SuccessFactors Recruiting Resume Shifter.
Implements scalable pagination ($top/$skip), per-application execution,
watermark preservation/advancement, CSV audit logging, and run metrics.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Generator, List, Optional

from client.exceptions import SFIntegrationError
from client.odata_client import SFODataClient
from config.settings import AppConfig
from core.models import (
    ApplicationStatus,
    BatchRunSummary,
    JobApplicationData,
    ProcessResult,
    RunStatus,
)
from core.processor import ApplicationProcessor
from core.watermark import WatermarkManager
from logging_utils.csv_logger import RunCSVLogger, SummaryCSVLogger

logger = logging.getLogger("ResumeShifter.BatchEngine")


class BatchEngine:
    """
    Enterprise batch processor coordinating discovery, execution,
    watermark tracking, and audit logging for Resume Shifter.
    """

    def __init__(
        self,
        config: AppConfig,
        client: SFODataClient,
        watermark_manager: Optional[WatermarkManager] = None,
        processor: Optional[ApplicationProcessor] = None,
    ) -> None:
        self.config = config
        self.client = client
        self.watermark_manager = watermark_manager or WatermarkManager(config.watermark_file_path)
        self.processor = processor or ApplicationProcessor(client)
        self.summary_logger = SummaryCSVLogger(config.summary_csv_path)

    def discover_applications(
        self,
        watermark: Optional[datetime] = None,
    ) -> Generator[JobApplicationData, None, None]:
        """
        Discovers all Job Applications since the given watermark using
        standard OData pagination ($top=1000, $skip=n).
        Yields applications one by one to support memory-efficient streaming.
        """
        top = self.config.batch_top
        skip = 0
        page_num = 1
        total_discovered = 0

        logger.info(
            "Starting application discovery (Watermark: %s, Page Size: %d)...",
            watermark.strftime("%Y-%m-%dT%H:%M:%SZ") if watermark else "None (Initial Full Run)",
            top,
        )

        while True:
            logger.debug("Fetching page %d (skip=%d, top=%d)...", page_num, skip, top)
            try:
                page_results = self.client.discover_applications_page(
                    watermark=watermark,
                    top=top,
                    skip=skip,
                )
            except Exception as e:
                logger.error("Failed to query JobApplication page at skip=%d: %s", skip, e)
                raise

            count_in_page = len(page_results)
            total_discovered += count_in_page
            logger.info("Page %d retrieved %d application record(s).", page_num, count_in_page)

            for app in page_results:
                yield app

            if count_in_page < top:
                # Reached the end of available records
                break

            skip += count_in_page
            page_num += 1

        logger.info("Discovery complete. Total applications discovered: %d", total_discovered)

    def process_application(
        self,
        application: JobApplicationData,
        run_timestamp_str: str,
        dry_run: bool = False,
        force: bool = False,
    ) -> ProcessResult:
        """Process a single application through the 7-step pipeline."""
        result = self.processor.process_application(
            application=application,
            run_timestamp=run_timestamp_str,
            dry_run=dry_run,
            force=force,
        )
        if self.config.rate_limit_pause_seconds > 0:
            time.sleep(self.config.rate_limit_pause_seconds)
        return result

    def run_single(
        self,
        application_id: str,
        dry_run: bool = False,
        force: bool = False,
    ) -> ProcessResult:
        """
        Execute processing for a single specified JobApplication ID.
        Useful for targeted testing, immediate triggers, or ad-hoc replays.
        """
        now = datetime.now(timezone.utc)
        run_ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_csv_logger = RunCSVLogger(self.config.logs_dir, run_ts_str)

        logger.info("Executing SINGLE APPLICATION mode for applicationId: %s (force=%s)", application_id, force)
        try:
            app_data = self.client.get_application(application_id, expand_cust_resume=True)
            result = self.process_application(
                application=app_data,
                run_timestamp_str=run_ts_str,
                dry_run=dry_run,
                force=force,
            )
        except Exception as e:
            logger.exception("Failed to retrieve or process single application %s: %s", application_id, e)
            result = ProcessResult(
                run_timestamp=run_ts_str,
                application_id=application_id,
                candidate_id="UNKNOWN",
                status=ApplicationStatus.FAILED,
                attachment_present=False,
                error_message=str(e),
            )

        run_csv_logger.write_row(result)
        return result

    def run(
        self,
        since_timestamp: Optional[datetime] = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> BatchRunSummary:
        """
        Execute the full scheduled batch run:
        1. Read watermark timestamp.
        2. Capture current run timestamp.
        3. Discover applications with $top=1000 and $skip pagination.
        4. Process every application individually.
        5. Write per-run CSV and summary CSV.
        6. Advance watermark ONLY if batch status = COMPLETED.
        """
        start_time = datetime.now(timezone.utc)
        run_ts_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Watermark resolution
        watermark = since_timestamp or self.watermark_manager.get_watermark()
        logger.info("================================================================================")
        logger.info("Starting Resume Shifter Batch Execution")
        logger.info("Run Timestamp: %s", run_ts_str)
        logger.info("Active Watermark: %s", watermark.strftime("%Y-%m-%dT%H:%M:%SZ") if watermark else "None")
        logger.info("Dry Run Mode: %s", dry_run)
        logger.info("================================================================================")

        run_csv_logger = RunCSVLogger(self.config.logs_dir, run_ts_str)

        summary = BatchRunSummary(
            run_timestamp=run_ts_str,
            applications_found=0,
            succeeded=0,
            skipped_already_set=0,
            skipped_no_resume=0,
            failed=0,
            run_status=RunStatus.COMPLETED,
        )

        try:
            # 2. Application Discovery & Iterative Processing
            for app in self.discover_applications(watermark=watermark):
                summary.applications_found += 1

                res = self.process_application(
                    application=app,
                    run_timestamp_str=run_ts_str,
                    dry_run=dry_run,
                )
                run_csv_logger.write_row(res)

                # Update counters
                if res.status == ApplicationStatus.SUCCESS:
                    summary.succeeded += 1
                elif res.status == ApplicationStatus.SKIPPED_ALREADY_SET:
                    summary.skipped_already_set += 1
                elif res.status == ApplicationStatus.SKIPPED_NO_RESUME:
                    summary.skipped_no_resume += 1
                elif res.status == ApplicationStatus.FAILED:
                    summary.failed += 1

        except Exception as exc:
            logger.exception("Fatal batch processing error during discovery or execution: %s", exc)
            summary.run_status = RunStatus.ERRORED
            summary.failed += 1

        # 3. Watermark Commit or Preservation
        if summary.run_status == RunStatus.COMPLETED and not dry_run:
            logger.info("Batch completed successfully. Advancing watermark to run timestamp: %s", run_ts_str)
            self.watermark_manager.save_watermark(start_time)
        elif summary.run_status == RunStatus.ERRORED:
            logger.warning(
                "Batch finished with ERRORED status. Watermark PRESERVED at previous state to enable replay."
            )
        elif dry_run:
            logger.info("[DRY RUN] Watermark not updated.")

        # 4. Record Run Summary
        self.summary_logger.record_summary(summary)

        # 5. Output Summary Log
        logger.info("================================================================================")
        logger.info("Resume Shifter Batch Execution Summary")
        logger.info("Run Status:            %s", summary.run_status.value)
        logger.info("Applications Found:    %d", summary.applications_found)
        logger.info("Succeeded:             %d", summary.succeeded)
        logger.info("Skipped (Already Set): %d", summary.skipped_already_set)
        logger.info("Skipped (No Resume):   %d", summary.skipped_no_resume)
        logger.info("Failed:                %d", summary.failed)
        logger.info("Log CSV:               %s", run_csv_logger.file_path)
        logger.info("Summary CSV:           %s", self.summary_logger.file_path)
        logger.info("================================================================================")

        return summary

    def orchestrate_resume_snapshot(
        self,
        application_id: Optional[str] = None,
        since_timestamp: Optional[datetime] = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> int:
        """
        High-level entrypoint orchestrating either single or batch runs.
        Returns CLI exit code (0 for SUCCESS/COMPLETED, 1 for ERRORED or FAILED).
        """
        if application_id:
            res = self.run_single(application_id=application_id, dry_run=dry_run, force=force)
            return 0 if res.status in [ApplicationStatus.SUCCESS, ApplicationStatus.SKIPPED_ALREADY_SET, ApplicationStatus.SKIPPED_NO_RESUME] else 1

        summary = self.run(since_timestamp=since_timestamp, dry_run=dry_run, force=force)
        if summary.run_status == RunStatus.ERRORED:
            return 1
        return 0
