"""
Batch Orchestration Engine for SAP SuccessFactors Recruiting Resume Shifter.
Implements scalable pagination ($top/$skip), concurrent multi-threaded execution,
watermark preservation/advancement, thread-safe CSV audit logging, and run metrics.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
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
    Supports both high-throughput multi-threaded execution and sequential processing.
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
        self.processor = processor or ApplicationProcessor(
            client=client,
            default_verify=config.verify_upsert,
        )
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
        verify: Optional[bool] = None,
    ) -> ProcessResult:
        """Process a single application through the 7-step pipeline."""
        result = self.processor.process_application(
            application=application,
            run_timestamp=run_timestamp_str,
            dry_run=dry_run,
            force=force,
            verify=verify,
        )
        if self.config.rate_limit_pause_seconds > 0:
            time.sleep(self.config.rate_limit_pause_seconds)
        return result

    def run_single(
        self,
        application_id: str,
        dry_run: bool = False,
        force: bool = False,
        verify: Optional[bool] = None,
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
                verify=verify,
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
        concurrency: Optional[int] = None,
        verify: Optional[bool] = None,
    ) -> BatchRunSummary:
        """
        Execute the full scheduled batch run:
        1. Read watermark timestamp.
        2. Capture current run timestamp.
        3. Discover applications with $top=1000 and $skip pagination.
        4. Process applications (concurrently via ThreadPoolExecutor or sequentially).
        5. Write thread-safe per-run CSV and summary CSV.
        6. Advance watermark ONLY if batch status = COMPLETED.
        """
        start_time = datetime.now(timezone.utc)
        run_ts_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        workers = concurrency if concurrency is not None else self.config.max_workers
        should_verify = verify if verify is not None else self.config.verify_upsert

        # 1. Watermark resolution
        watermark = since_timestamp or self.watermark_manager.get_watermark()
        logger.info("================================================================================")
        logger.info("Starting Resume Shifter Batch Execution")
        logger.info("Run Timestamp: %s", run_ts_str)
        logger.info("Active Watermark: %s", watermark.strftime("%Y-%m-%dT%H:%M:%SZ") if watermark else "None")
        logger.info("Dry Run Mode:  %s", dry_run)
        logger.info("Concurrency:   %d worker(s)", workers)
        logger.info("Verify Upsert: %s", should_verify)
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

        summary_lock = threading.Lock()

        def _update_summary(res: ProcessResult) -> None:
            with summary_lock:
                if res.status == ApplicationStatus.SUCCESS:
                    summary.succeeded += 1
                elif res.status == ApplicationStatus.SKIPPED_ALREADY_SET:
                    summary.skipped_already_set += 1
                elif res.status == ApplicationStatus.SKIPPED_NO_RESUME:
                    summary.skipped_no_resume += 1
                elif res.status == ApplicationStatus.FAILED:
                    summary.failed += 1

        try:
            # 2. Application Discovery & Streamed Execution
            with run_csv_logger:
                if workers <= 1:
                    # Sequential execution mode
                    for app in self.discover_applications(watermark=watermark):
                        summary.applications_found += 1
                        res = self.process_application(
                            application=app,
                            run_timestamp_str=run_ts_str,
                            dry_run=dry_run,
                            force=force,
                            verify=should_verify,
                        )
                        run_csv_logger.write_row(res)
                        _update_summary(res)
                else:
                    # Concurrent multi-threaded execution mode with bounded worker pool
                    max_in_flight = workers * 4
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                        futures_map: dict[concurrent.futures.Future, str] = {}

                        def _flush_completed(wait_for_one: bool = False) -> None:
                            done_futures = set()
                            if wait_for_one and futures_map:
                                done, _ = concurrent.futures.wait(
                                    futures_map.keys(),
                                    return_when=concurrent.futures.FIRST_COMPLETED,
                                )
                                done_futures = done
                            else:
                                done_futures = {f for f in futures_map if f.done()}

                            for fut in done_futures:
                                app_id_ref = futures_map.pop(fut)
                                try:
                                    res = fut.result()
                                    run_csv_logger.write_row(res)
                                    _update_summary(res)
                                except Exception as task_exc:
                                    logger.exception("Unexpected exception in worker thread for application %s: %s", app_id_ref, task_exc)
                                    fail_res = ProcessResult(
                                        run_timestamp=run_ts_str,
                                        application_id=app_id_ref,
                                        candidate_id="UNKNOWN",
                                        status=ApplicationStatus.FAILED,
                                        attachment_present=False,
                                        error_message=f"Worker failure: {task_exc}",
                                    )
                                    run_csv_logger.write_row(fail_res)
                                    _update_summary(fail_res)

                        for app in self.discover_applications(watermark=watermark):
                            summary.applications_found += 1
                            # Backpressure: ensure memory doesn't grow unbounded
                            while len(futures_map) >= max_in_flight:
                                _flush_completed(wait_for_one=True)

                            fut = executor.submit(
                                self.process_application,
                                application=app,
                                run_timestamp_str=run_ts_str,
                                dry_run=dry_run,
                                force=force,
                                verify=should_verify,
                            )
                            futures_map[fut] = app.application_id

                        # Drain all remaining in-flight tasks
                        while futures_map:
                            _flush_completed(wait_for_one=True)

        except Exception as exc:
            logger.exception("Fatal batch processing error during discovery or execution: %s", exc)
            summary.run_status = RunStatus.ERRORED
            summary.failed += 1
        end_time = datetime.now(timezone.utc)
        summary.elapsed_seconds = round((end_time - start_time).total_seconds(), 2)

        # 3. Watermark Commit or Preservation (All-or-Nothing check)
        all_succeeded = (summary.run_status == RunStatus.COMPLETED and summary.failed == 0)

        if all_succeeded and not dry_run:
            logger.info("Batch completed successfully (all %d succeeded/skipped, 0 failed). Advancing watermark to run start timestamp: %s", summary.applications_found, run_ts_str)
            self.watermark_manager.save_watermark(start_time)
        elif not all_succeeded:
            if summary.failed > 0 and summary.run_status == RunStatus.COMPLETED:
                summary.run_status = RunStatus.ERRORED
            logger.warning(
                "Batch finished with %s status (failed=%d). Watermark PRESERVED at previous state to enable retry on next run.",
                summary.run_status.value,
                summary.failed,
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
        logger.info("Elapsed Time:          %.2fs", summary.elapsed_seconds)
        logger.info("Throughput:            %.2f records/sec", summary.throughput_records_per_sec)
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
        concurrency: Optional[int] = None,
        verify: Optional[bool] = None,
    ) -> int:
        """
        High-level entrypoint orchestrating either single or batch runs.
        Returns CLI exit code (0 for SUCCESS/COMPLETED, 1 for ERRORED or FAILED).
        """
        if application_id:
            res = self.run_single(
                application_id=application_id,
                dry_run=dry_run,
                force=force,
                verify=verify,
            )
            return 0 if res.status in [ApplicationStatus.SUCCESS, ApplicationStatus.SKIPPED_ALREADY_SET, ApplicationStatus.SKIPPED_NO_RESUME] else 1

        summary = self.run(
            since_timestamp=since_timestamp,
            dry_run=dry_run,
            force=force,
            concurrency=concurrency,
            verify=verify,
        )
        if summary.run_status == RunStatus.ERRORED:
            return 1
        return 0
