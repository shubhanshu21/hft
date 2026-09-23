"""
dashboard/server.py — Real-time Web Observability Dashboard Server for HFT Engine.

Serves live portfolio metrics, real-time PnL, open positions, equity curves,
and system status over HTTP and Server-Sent Events (SSE).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import TradingDB
from utils.logger import get_logger

log = get_logger("dashboard")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP request handler providing REST API and static UI dashboard."""

    db: TradingDB = TradingDB()
    account_id: str = "DRYRUN_ACCOUNT"

    def do_GET(self) -> None:
        if self.path.startswith("/api/summary"):
            self._handle_api_summary()
        elif self.path.startswith("/api/stream"):
            self._handle_api_stream()
        elif self.path == "/" or self.path == "/index.html":
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path.startswith("/static/"):
            rel_path = self.path.replace("/static/", "", 1).split("?")[0]
            file_path = STATIC_DIR / rel_path
            self._serve_file(file_path)
        else:
            self.send_error(404, "Not Found")

    def _handle_api_summary(self) -> None:
        try:
            data = self.db.get_dashboard_summary(self.account_id)
            payload = json.dumps(data, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            log.error("API summary error: %s", e)
            self.send_error(500, str(e))

    def _handle_api_stream(self) -> None:
        """Server-Sent Events (SSE) stream pushing updates to UI."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                data = self.db.get_dashboard_summary(self.account_id)
                payload = f"data: {json.dumps(data, default=str)}\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                time.sleep(2.0)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_file(self, path: Path, content_type: Optional[str] = None) -> None:
        if not path.exists() or path.is_dir():
            self.send_error(404, "File not found")
            return

        if not content_type:
            if path.suffix == ".html":
                content_type = "text/html; charset=utf-8"
            elif path.suffix == ".css":
                content_type = "text/css"
            elif path.suffix == ".js":
                content_type = "application/javascript"
            elif path.suffix == ".json":
                content_type = "application/json"
            else:
                content_type = "application/octet-stream"

        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass  # Suppress noisy HTTP request logging


def run_dashboard_server(port: int = 8080, account_id: str = "DRYRUN_ACCOUNT", db_path: Optional[str] = None) -> None:
    """Launches the HTTP dashboard server on the given port."""
    DashboardHandler.db = TradingDB(db_path)
    DashboardHandler.account_id = account_id
    server_address = ("", port)
    httpd = HTTPServer(server_address, DashboardHandler)
    print(f"\n=======================================================")
    print(f"🚀 HFT Web Monitoring Dashboard running on http://127.0.0.1:{port}")
    print(f"   Account: {account_id} | Database: {DashboardHandler.db.db_path}")
    print(f"=======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HFT Web Observability Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind dashboard (default: 8080)")
    parser.add_argument("--account", default="DRYRUN_ACCOUNT", help="Account ID to display (default: DRYRUN_ACCOUNT)")
    parser.add_argument("--db", default=None, help="Path to database")
    args = parser.parse_args()
    run_dashboard_server(port=args.port, account_id=args.account, db_path=args.db)
