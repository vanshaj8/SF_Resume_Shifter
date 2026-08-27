#!/usr/bin/env python3
"""
Hourly Background Batch Scheduler for SAP SuccessFactors Resume Shifter.

Runs the batch synchronization process on a recurring interval (default: 3600s / 1 hour)
using the watermark mechanism for incremental discovery.
Supports graceful shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from client.auth import create_auth_provider
from client.odata_client import SFODataClient
from config.settings import AppConfig, get_config
from core.engine import BatchEngine
from core.processor import ApplicationProcessor
from core.watermark import WatermarkManager
from logging_utils.logger import setup_logging

logger = logging.getLogger("ResumeShifter.Scheduler")


class ResumeShifterScheduler:
    """Daemon scheduler running Resume Shifter batch synchronization on a fixed interval."""

    def __init__(self, interval_seconds: int = 3600, mock: bool = False, concurrency: int = 5) -> None:
        self.interval_seconds = interval_seconds
        self.mock = mock
        self.concurrency = concurrency
        self._running = False
        self.config: AppConfig = get_config()
        self.config.max_workers = concurrency

        # Signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        logger.info("Received termination signal (%s). Shutting down scheduler gracefully...", signum)
        self._running = False

    def start(self) -> None:
        """Start the continuous scheduler loop."""
        setup_logging(log_level=self.config.log_level, logs_dir=self.config.logs_dir)
        logger.info("================================================================================")
        logger.info("Starting SAP SuccessFactors Resume Shifter Daemon Scheduler")
        logger.info("Execution Interval: %d seconds (%.1f hour(s))", self.interval_seconds, self.interval_seconds / 3600.0)
        logger.info("Concurrency:        %d workers", self.concurrency)
        logger.info("Mode:               %s", "MOCK" if self.mock else "LIVE SAP OData")
        logger.info("================================================================================")

        self._running = True
        iteration = 1

        while self._running:
            run_start = time.time()
            logger.info(">>> [Scheduler Iteration #%d] Triggering batch run at %s", iteration, datetime.now(timezone.utc).isoformat())

            try:
                # Initialize fresh client and engine for each run
                if self.mock:
                    from tests.mock_sf_server import create_mock_sf_client
                    client = create_mock_sf_client(self.config)
                else:
                    auth_provider = create_auth_provider(self.config)
                    client = SFODataClient(config=self.config, auth_provider=auth_provider)

                watermark_mgr = WatermarkManager(self.config.watermark_file_path)
                processor = ApplicationProcessor(client=client, default_verify=self.config.verify_upsert)
                engine = BatchEngine(
                    config=self.config,
                    client=client,
                    watermark_manager=watermark_mgr,
                    processor=processor,
                )

                summary = engine.run(concurrency=self.concurrency)
                logger.info(
                    "<<< [Scheduler Iteration #%d Completed] Status: %s | Found: %d | Succeeded: %d | Skipped: %d | Failed: %d in %.2fs",
                    iteration,
                    summary.run_status.value,
                    summary.applications_found,
                    summary.succeeded,
                    summary.skipped_already_set + summary.skipped_no_resume,
                    summary.failed,
                    summary.elapsed_seconds,
                )
            except Exception as e:
                logger.exception("Error executing scheduled batch iteration #%d: %s", iteration, e)

            iteration += 1

            # Sleep until next interval, checking _running flag every 1 second
            elapsed = time.time() - run_start
            sleep_time = max(1.0, self.interval_seconds - elapsed)
            logger.info("Sleeping for %d seconds until next scheduled run...", int(sleep_time))

            sleep_end = time.time() + sleep_time
            while self._running and time.time() < sleep_end:
                time.sleep(1.0)

        logger.info("Scheduler stopped cleanly.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume Shifter Recurring Daemon Scheduler")
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        metavar="SECONDS",
        help="Interval between batch runs in seconds (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=5,
        help="Number of concurrent worker threads (default: 5)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in mock mode for offline testing",
    )
    args = parser.parse_args()

    scheduler = ResumeShifterScheduler(
        interval_seconds=args.interval,
        mock=args.mock,
        concurrency=args.concurrency,
    )
    scheduler.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
