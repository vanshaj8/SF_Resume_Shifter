"""
Unit tests for Real-Time Webhook Server.
"""

from __future__ import annotations

import json
from http.server import HTTPServer
import threading
import time
import requests
import pytest

from config.settings import AppConfig
from core.engine import BatchEngine
from core.processor import ApplicationProcessor
from core.watermark import WatermarkManager
from tests.mock_sf_server import MockSFDatabase, MockSFODataClient
from webhook_server import WebhookRequestHandler


@pytest.fixture
def mock_webhook_server(tmp_path):
    wm_file = tmp_path / "watermark.txt"
    logs_dir = tmp_path / "logs"
    summary_file = tmp_path / "summary.csv"
    cfg = AppConfig(
        watermark_file_path=wm_file,
        logs_dir=logs_dir,
        summary_csv_path=summary_file,
        rate_limit_pause_seconds=0.0,
    )
    db = MockSFDatabase()
    client = MockSFODataClient(config=cfg, db=db)
    wm_mgr = WatermarkManager(wm_file)
    processor = ApplicationProcessor(client)
    engine = BatchEngine(
        config=cfg,
        client=client,
        watermark_manager=wm_mgr,
        processor=processor,
    )

    WebhookRequestHandler.config = cfg
    WebhookRequestHandler.engine = engine

    # Use port 0 to bind to an available ephemeral port
    server = HTTPServer(("127.0.0.1", 0), WebhookRequestHandler)
    port = server.server_address[1]

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    server.server_close()


def test_webhook_health_check(mock_webhook_server):
    res = requests.get(f"{mock_webhook_server}/health", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "HEALTHY"


def test_webhook_application_created_trigger(mock_webhook_server):
    payload = {"applicationId": "1001"}
    res = requests.post(
        f"{mock_webhook_server}/webhook/application-created",
        json=payload,
        timeout=5,
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "ACCEPTED"
    assert data["applicationId"] == "1001"

    # Give background thread a moment to complete
    time.sleep(0.5)


def test_webhook_sap_isc_payload_format(mock_webhook_server):
    # SAP Intelligent Services format
    payload = {
        "events": [
            {
                "eventId": "evt-12345",
                "entityKeys": [
                    {"key": "applicationId", "value": "1004"}
                ]
            }
        ]
    }
    res = requests.post(
        f"{mock_webhook_server}/webhook/application-created",
        json=payload,
        timeout=5,
    )
    assert res.status_code == 202
    assert res.json()["applicationId"] == "1004"
