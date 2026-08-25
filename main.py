#!/usr/bin/env python3
"""
SAP SuccessFactors Recruiting Resume Shifter CLI.

Automated integration copying Candidate Profile resumes into Job Application
custom attachment fields (JobApplication.Cust_Candidate_Resume) with strict
WRITE-ONCE historical preservation.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as date_parser

from client.auth import create_auth_provider
from client.odata_client import SFODataClient
from config.settings import AppConfig, get_config
from core.engine import BatchEngine
from core.processor import ApplicationProcessor
from core.watermark import WatermarkManager
from logging_utils.logger import setup_logging


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="SAP SuccessFactors Recruiting Resume Shifter - Candidate Resume Snapshot Integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Mode 1: Single application execution
  python main.py --single 1001

  # Mode 2: Scheduled batch synchronization
  python main.py --batch

  # Mode 3: Batch synchronization with dry-run simulation
  python main.py --batch --dry-run

  # Mode 4: Batch run overriding watermark starting date
  python main.py --batch --since "2026-01-01T00:00:00Z"

  # Mode 5: Inspect current watermark
  python main.py --get-watermark

  # Mode 6: Reset watermark timestamp
  python main.py --reset-watermark "2026-08-01T00:00:00Z"

  # Mode 7: Offline simulation with built-in Mock SuccessFactors Server
  python main.py --batch --mock
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--single",
        type=str,
        metavar="APPLICATION_ID",
        help="Process a single Job Application by ID (Write-Once check, fetch resume, upsert, verify)",
    )
    group.add_argument(
        "--batch",
        action="store_true",
        help="Execute scheduled batch synchronization using stored watermark",
    )
    group.add_argument(
        "--get-watermark",
        action="store_true",
        help="Display the currently saved watermark timestamp from watermark.txt",
    )
    group.add_argument(
        "--reset-watermark",
        type=str,
        metavar="TIMESTAMP_OR_CLEAR",
        help="Reset watermark to given ISO-8601 timestamp (e.g. '2026-01-01T00:00:00Z') or 'clear'",
    )

    parser.add_argument(
        "--since",
        type=str,
        metavar="TIMESTAMP",
        help="Override watermark filter starting timestamp for this batch run (e.g. '2026-01-01T00:00:00Z')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without modifying SAP SuccessFactors records or advancing watermark",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force copy even if Cust_Candidate_Resume is already populated (testing/override)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use built-in mock SuccessFactors OData v2 engine for offline validation",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override logging verbosity level",
    )

    return parser.parse_args()


def main() -> int:
    """Main CLI execution flow."""
    args = parse_args()
    config = get_config()

    if args.log_level:
        config.log_level = args.log_level

    logger = setup_logging(
        log_level=config.log_level,
        logs_dir=config.logs_dir,
    )

    watermark_mgr = WatermarkManager(config.watermark_file_path)

    # ---------------------------------------------------------
    # Utility Commands: Get / Reset Watermark
    # ---------------------------------------------------------
    if args.get_watermark:
        wm = watermark_mgr.get_watermark()
        if wm:
            print(f"Current Watermark: {wm.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        else:
            print("No watermark currently stored (initial run will process all records).")
        return 0

    if args.reset_watermark:
        try:
            watermark_mgr.reset_watermark(args.reset_watermark)
            print(f"Watermark updated successfully to: {args.reset_watermark}")
            return 0
        except Exception as e:
            logger.error("Failed to reset watermark: %s", e)
            return 1

    # ---------------------------------------------------------
    # Parse --since timestamp if provided
    # ---------------------------------------------------------
    since_dt = None
    if args.since:
        try:
            since_dt = date_parser.isoparse(args.since)
            if not since_dt.tzinfo:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            else:
                since_dt = since_dt.astimezone(timezone.utc)
        except Exception as e:
            logger.error("Invalid --since timestamp '%s': %s", args.since, e)
            return 1

    # ---------------------------------------------------------
    # Initialize Auth & Client
    # ---------------------------------------------------------
    if args.mock:
        logger.info("Initializing in MOCK mode with in-memory SuccessFactors test database.")
        from tests.mock_sf_server import create_mock_sf_client
        client = create_mock_sf_client(config)
    else:
        try:
            auth_provider = create_auth_provider(config)
            client = SFODataClient(config=config, auth_provider=auth_provider)
        except Exception as e:
            logger.error("Failed to initialize SuccessFactors OData client: %s", e)
            return 1

    processor = ApplicationProcessor(client)
    engine = BatchEngine(
        config=config,
        client=client,
        watermark_manager=watermark_mgr,
        processor=processor,
    )

    # ---------------------------------------------------------
    # Execute Mode
    # ---------------------------------------------------------
    if args.single:
        return engine.orchestrate_resume_snapshot(
            application_id=args.single,
            dry_run=args.dry_run,
            force=args.force,
        )

    if args.batch:
        return engine.orchestrate_resume_snapshot(
            since_timestamp=since_dt,
            dry_run=args.dry_run,
            force=args.force,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
