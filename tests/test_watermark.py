"""
Unit tests for WatermarkManager.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from core.watermark import WatermarkManager


def test_watermark_read_non_existent_file(tmp_path):
    wm_file = tmp_path / "watermark_non_existent.txt"
    mgr = WatermarkManager(wm_file)
    assert mgr.get_watermark() is None


def test_watermark_save_and_read(tmp_path):
    wm_file = tmp_path / "watermark.txt"
    mgr = WatermarkManager(wm_file)

    test_dt = datetime(2026, 8, 25, 12, 34, 56, tzinfo=timezone.utc)
    mgr.save_watermark(test_dt)

    read_dt = mgr.get_watermark()
    assert read_dt is not None
    assert read_dt == test_dt

    with open(wm_file, "r") as f:
        assert f.read().strip() == "2026-08-25T12:34:56Z"


def test_watermark_reset(tmp_path):
    wm_file = tmp_path / "watermark.txt"
    mgr = WatermarkManager(wm_file)

    test_dt = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
    mgr.save_watermark(test_dt)
    assert mgr.get_watermark() is not None

    # Reset to new timestamp
    mgr.reset_watermark("2026-01-01T00:00:00Z")
    assert mgr.get_watermark() == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Clear watermark
    mgr.reset_watermark("clear")
    assert mgr.get_watermark() is None
    assert not wm_file.exists()
