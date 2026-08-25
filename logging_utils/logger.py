"""
Logging setup and secret redaction filters for SAP SuccessFactors integration.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional


class SensitiveDataFilter(logging.Filter):
    """Redacts sensitive tokens, private keys, and passwords from log records."""

    PATTERNS = [
        (re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE), "Bearer [REDACTED]"),
        (re.compile(r"Basic\s+[A-Za-z0-9\+/=]+", re.IGNORECASE), "Basic [REDACTED]"),
        (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----", re.DOTALL), "[PRIVATE KEY REDACTED]"),
        (re.compile(r'"fileContent"\s*:\s*"[^"]+"'), '"fileContent": "[BASE64 CONTENT REDACTED]"'),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern, repl in self.PATTERNS:
                record.msg = pattern.sub(repl, record.msg)
        if record.args:
            clean_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    for pattern, repl in self.PATTERNS:
                        arg = pattern.sub(repl, arg)
                clean_args.append(arg)
            record.args = tuple(clean_args)
        return True


def setup_logging(
    log_level: str = "INFO",
    logs_dir: Optional[Path] = None,
    log_file_prefix: str = "resume_shifter",
) -> logging.Logger:
    """Configures structured console and file logging."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger("ResumeShifter")
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()

    sensitive_filter = SensitiveDataFilter()

    # Console Handler
    console_formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(sensitive_filter)
    root_logger.addHandler(console_handler)

    # File Handler
    if logs_dir:
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / f"{log_file_prefix}.log"
        file_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(pathname)s:%(lineno)d: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(file_formatter)
        file_handler.addFilter(sensitive_filter)
        root_logger.addHandler(file_handler)

    return root_logger
