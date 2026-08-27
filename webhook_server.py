#!/usr/bin/env python3
"""
Real-Time Webhook Listener for SAP SuccessFactors Recruiting Resume Shifter.

Receives inbound HTTP POST webhooks when a new Job Application is created
(e.g., from SAP SuccessFactors Intelligent Services Center / Integration Center / SAP CPI / REST trigger),
and immediately executes the resume snapshot copy for that application ID.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

from client.auth import create_auth_provider
from client.odata_client import SFODataClient
from config.settings import AppConfig, get_config
from core.engine import BatchEngine
from core.models import ApplicationStatus
from core.processor import ApplicationProcessor
from core.watermark import WatermarkManager
from logging_utils.logger import setup_logging

logger = logging.getLogger("ResumeShifter.WebhookServer")


class WebhookRequestHandler(BaseHTTPRequestHandler):
    """Handles incoming JSON webhook requests from SAP SuccessFactors or external orchestrators."""

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
    config: AppConfig
    engine: BatchEngine

    def _send_json_response(self, status_code: int, payload: dict[str, Any]) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_GET(self) -> None:
        """Health check endpoint: GET /health"""
        if self.path in ("/health", "/", "/api/v1/health"):
            self._send_json_response(200, {
                "status": "HEALTHY",
                "service": "ResumeShifter-Webhook-Receiver",
                "version": "1.0.0",
            })
        else:
            self._send_json_response(404, {"error": "Not Found"})

    def do_POST(self) -> None:
        """
        Webhook trigger endpoint:
        POST /webhook/application-created
        POST /api/v1/applications/process

        Expected payload formats:
        1. {"applicationId": "1001"}
        2. {"id": "1001"}
        3. SAP ISC Event Payload: {"events": [{"entityKeys": [{"key": "applicationId", "value": "1001"}]}]}
        """
        valid_paths = ("/webhook/application-created", "/api/v1/applications/process", "/webhook")
        if not any(self.path.startswith(p) for p in valid_paths):
            self._send_json_response(404, {"error": f"Endpoint '{self.path}' not found."})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_response(400, {"error": "Empty request body."})
            return

        try:
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
        except Exception as e:
            logger.error("Failed to parse incoming webhook JSON: %s", e)
            self._send_json_response(400, {"error": f"Malformed JSON: {e}"})
            return

        # Extract applicationId from various standard formats
        app_id = self._extract_application_id(data)
        if not app_id:
            self._send_json_response(400, {
                "error": "Missing 'applicationId' in request body.",
                "received_payload": data,
            })
            return

        logger.info("[Webhook Trigger Received] Enqueuing real-time processing for applicationId: %s", app_id)

        # Dispatch execution in background thread pool to respond immediately (HTTP 202 Accepted)
        self.executor.submit(self._process_application_async, str(app_id))

        self._send_json_response(202, {
            "status": "ACCEPTED",
            "message": f"Resume snapshot integration enqueued for JobApplication {app_id}",
            "applicationId": str(app_id),
        })

    @classmethod
    def _extract_application_id(cls, data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None

        # Format 1: Direct key
        if "applicationId" in data and data["applicationId"]:
            return str(data["applicationId"])
        if "application_id" in data and data["application_id"]:
            return str(data["application_id"])
        if "id" in data and data["id"]:
            return str(data["id"])

        # Format 2: SAP ISC Event payload
        events = data.get("events")
        if isinstance(events, list) and len(events) > 0:
            first_event = events[0]
            if isinstance(first_event, dict):
                entity_keys = first_event.get("entityKeys", [])
                for ek in entity_keys:
                    if isinstance(ek, dict) and ek.get("key") == "applicationId":
                        return str(ek.get("value"))

        return None

    @classmethod
    def _process_application_async(cls, application_id: str) -> None:
        try:
            logger.info("Executing real-time resume copy for applicationId: %s", application_id)
            res = cls.engine.run_single(application_id=application_id)
            logger.info(
                "Real-time processing completed for applicationId: %s | Status: %s | Attachment: %s",
                application_id,
                res.status.value,
                res.attachment_present,
            )
        except Exception as e:
            logger.exception("Async webhook execution failed for applicationId %s: %s", application_id, e)


def run_webhook_server(host: str = "0.0.0.0", port: int = 8080, mock: bool = False) -> None:
    """Start the HTTP webhook server."""
    config = get_config()
    setup_logging(log_level=config.log_level, logs_dir=config.logs_dir)

    if mock:
        logger.info("Initializing Webhook Server in MOCK mode.")
        from tests.mock_sf_server import create_mock_sf_client
        client = create_mock_sf_client(config)
    else:
        auth_provider = create_auth_provider(config)
        client = SFODataClient(config=config, auth_provider=auth_provider)

    watermark_mgr = WatermarkManager(config.watermark_file_path)
    processor = ApplicationProcessor(client=client, default_verify=config.verify_upsert)
    engine = BatchEngine(
        config=config,
        client=client,
        watermark_manager=watermark_mgr,
        processor=processor,
    )

    WebhookRequestHandler.config = config
    WebhookRequestHandler.engine = engine

    server_address = (host, port)
    httpd = HTTPServer(server_address, WebhookRequestHandler)

    logger.info("================================================================================")
    logger.info("Resume Shifter Real-Time Webhook Server Running on http://%s:%d", host, port)
    logger.info("Endpoints:")
    logger.info("  GET  /health                   -> Health check")
    logger.info("  POST /webhook/application-created -> Real-time application trigger")
    logger.info("================================================================================")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Webhook Server...")
        httpd.server_close()
        logger.info("Webhook Server stopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume Shifter Real-Time Webhook Listener")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode for offline testing")
    args = parser.parse_args()

    run_webhook_server(host=args.host, port=args.port, mock=args.mock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
