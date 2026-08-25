"""
Watermark State Manager for SAP SuccessFactors Batch Synchronization.
Manages persistent state in watermark.txt, atomic writes, and replay safety.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dateutil import parser as date_parser

from client.exceptions import SFWatermarkError

logger = logging.getLogger("ResumeShifter.Watermark")


class WatermarkManager:
    """
    Manages the synchronization watermark timestamp.
    Ensures safe persistence, ISO-8601 parsing, and atomic file updates.
    """

    def __init__(self, watermark_file_path: Path | str = "watermark.txt") -> None:
        self.file_path = Path(watermark_file_path).resolve()

    def get_watermark(self) -> Optional[datetime]:
        """
        Read and parse the watermark timestamp from file.
        Returns None if file does not exist or is empty.
        """
        if not self.file_path.exists():
            logger.info("Watermark file does not exist at '%s'. Initial run without watermark.", self.file_path)
            return None

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()

            if not content:
                logger.info("Watermark file '%s' is empty.", self.file_path)
                return None

            dt = date_parser.isoparse(content)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

            logger.info("Loaded watermark: %s", dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            return dt
        except Exception as e:
            logger.warning(
                "Failed to parse watermark from '%s' (%s). Treating as no watermark.",
                self.file_path,
                e,
            )
            return None

    def save_watermark(self, dt: datetime) -> None:
        """
        Atomically persist a new watermark timestamp to file.
        Always stores ISO-8601 UTC string (e.g. 2026-08-25T12:00:00Z).
        """
        utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        iso_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Ensure parent directory exists
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Atomic write via temporary file in same folder
            parent_dir = self.file_path.parent
            with tempfile.NamedTemporaryFile(
                "w",
                dir=parent_dir,
                delete=False,
                encoding="utf-8",
            ) as tf:
                tf.write(f"{iso_str}\n")
                temp_name = tf.name

            os.replace(temp_name, self.file_path)
            logger.info("Committed new watermark: %s to %s", iso_str, self.file_path)
        except Exception as e:
            raise SFWatermarkError(f"Failed to atomically persist watermark to '{self.file_path}': {e}") from e

    def reset_watermark(self, timestamp_str: Optional[str] = None) -> None:
        """
        Reset watermark to a specific timestamp or clear it.
        """
        if not timestamp_str or timestamp_str.strip().lower() in ["none", "clear", ""]:
            if self.file_path.exists():
                self.file_path.unlink()
                logger.info("Cleared watermark file at %s", self.file_path)
            return

        dt = date_parser.isoparse(timestamp_str.strip())
        self.save_watermark(dt)
