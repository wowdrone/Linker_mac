"""Common loopback HTTP layer for the Linker browser dashboards (Legacy drive / Tracking).

Policy shared by every dashboard:
- binds to 127.0.0.1 only;
- every request must carry a loopback Host header for this port (DNS rebinding guard);
- protected GETs need the per-process X-Dashboard-Token;
- POST /command needs Host, matching Origin and the token, a JSON object body of at most
  MAX_BODY bytes, and passes the subclass validator before it is queued;
- the command queue is bounded; it is drained by the owner (Qt) thread, never executed on
  the HTTP thread.
"""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading

from guide_protocol import endpoint


class LoopbackServer:
    MAX_BODY = 4096
    QUEUE = 16
    PROTECTED = ()
    THREAD_NAME = "dashboard"

    def __init__(self, bind):
        host, port = endpoint(bind)
        if host != "127.0.0.1":
            raise ValueError("The dashboard only serves on loopback")
        self._cond = threading.Condition()
        self._closed = False
        self.token = secrets.token_urlsafe(32)
        self.commands = deque()
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name=self.THREAD_NAME, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def hosts(self):
        return (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()

    def drain(self):
        with self._cond:
            commands = list(self.commands)
            self.commands.clear()
        return commands

    # Subclass hooks ---------------------------------------------------------
    def handle_get(self, handler, path):
        """Return True if handled. Host (and token for PROTECTED paths) already checked."""
        return False

    def validate_command(self, command):
        """Return the normalised command or raise ValueError."""
        if not isinstance(command, dict):
            raise ValueError("Expected an object")
        return command

    # ------------------------------------------------------------------------
    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, status, kind, body, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            _send = send   # legacy name

            def forbidden(self):
                self.send(403, "text/plain; charset=utf-8", b"forbidden")

            def do_GET(self):
                # Reject foreign Host headers so other sites cannot read this feed via DNS rebinding.
                if self.headers.get("Host") not in server.hosts():
                    return self.forbidden()
                path = self.path.split("?", 1)[0]
                if path in server.PROTECTED and self.headers.get("X-Dashboard-Token") != server.token:
                    return self.forbidden()
                if not server.handle_get(self, path):
                    self.send(404, "text/plain; charset=utf-8", b"not found")

            def do_POST(self):
                host = self.headers.get("Host")
                if (host not in server.hosts() or self.headers.get("Origin") != f"http://{host}" or
                        self.headers.get("X-Dashboard-Token") != server.token):
                    return self.send(403, "text/plain", b"forbidden")
                if self.path != "/command":
                    return self.send(404, "text/plain", b"not found")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= server.MAX_BODY:
                        raise ValueError("Invalid request size")
                    command = server.validate_command(json.loads(self.rfile.read(size)))
                except (ValueError, TypeError, KeyError):
                    return self.send(400, "text/plain", b"invalid request")
                with server._cond:
                    if len(server.commands) >= server.QUEUE:
                        return self.send(429, "text/plain", b"queue full")
                    server.commands.append(command)
                self.send(202, "application/json", b'{"queued":true}')

        return Handler
