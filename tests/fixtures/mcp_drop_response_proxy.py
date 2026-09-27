"""Local eval fixture: apply one Hoard write, then lose its reply.

The backing Hoard must use isolated data. The proxy never changes requests;
it cuts the first successful response for the selected tool after the backing
app has committed it. ``GET /__stats`` reports forwarded/dropped calls.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(origin: str, port: int, drop_tool: str) -> None:
    lock = threading.Lock()
    stats = {"forwarded": 0, "dropped": 0, "names": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/__stats":
                self.send_error(404)
                return
            body = json.dumps(stats).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/api/agent/call":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                name = str(json.loads(raw).get("name") or "")
            except (ValueError, AttributeError):
                name = ""
            request = urllib.request.Request(
                origin + self.path, data=raw, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": self.headers.get("Authorization", "")},
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    status, body = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
            except Exception as exc:  # noqa: BLE001 - eval fixture reports upstream failure
                status, body = 502, json.dumps({"error": str(exc)}).encode()
            with lock:
                stats["forwarded"] += 1
                stats["names"].append(name)
                drop = name == drop_tool and status < 400 and stats["dropped"] == 0
                if drop:
                    stats["dropped"] += 1
            if drop:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--drop-tool", required=True)
    args = parser.parse_args()
    if not args.origin.startswith("http://127.0.0.1:"):
        parser.error("--origin must be a loopback HTTP service")
    serve(args.origin.rstrip("/"), args.port, args.drop_tool)
