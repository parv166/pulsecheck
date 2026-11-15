import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATUS_FILE = Path("/tmp/health-status")


def current_status():
    try:
        status = int(STATUS_FILE.read_text().strip())
    except FileNotFoundError:
        return 200

    return status if status in {200, 503} else 503


class HealthHandler(BaseHTTPRequestHandler):

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/container-health":
            self.send_json(200, {"container": "healthy"})
            return

        if self.path == "/health":
            status = current_status()
            self.send_json(
                status,
                {
                    "status": "healthy" if status == 200 else "unhealthy",
                    "application": "PulseCheck Test Simulator",
                    "http_status": status,
                },
            )
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/control":
            self.send_json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024:
                raise ValueError("Invalid request size.")

            payload = json.loads(self.rfile.read(length))
            status = payload.get("status")

            if status not in (200, 503):
                raise ValueError("Only HTTP 200 and 503 are supported.")

            STATUS_FILE.write_text(str(status))
            self.send_json(200, {"configured_status": status})

        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid control request."})

    def log_message(self, format, *args):
        print(
            f"{self.address_string()} - {format % args}",
            flush=True,
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer(
        ("0.0.0.0", 8080),
        HealthHandler,
    )
    print("Health simulator listening on port 8080", flush=True)
    server.serve_forever()
