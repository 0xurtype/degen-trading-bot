#!/usr/bin/env python3
"""
Static file server + API proxy for GMGN-scanner.
Serves public/ on port 3000, proxies /api/* -> backend on 8000.
"""
import http.server
import urllib.request
import urllib.error
import os
import re
import sys

PUBLIC_DIR = "/tmp/GMGN-scanner/public"
BACKEND = "http://127.0.0.1:8000"
PROXY_PORT = 3000


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_PROXY(self, method):
        """Proxy request to backend."""
        path = self.path
        url = f"{BACKEND}{path}"
        body = None
        if "Content-Length" in self.headers:
            body = self.rfile.read(int(self.headers["Content-Length"]))

        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "transfer-encoding")
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.copyfile(resp, self.wfile)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.copyfile(e.fp, self.wfile)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f'{{"error":"proxy error: {e}"}}'.encode())

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.do_PROXY("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self.do_PROXY("POST")
        return super().do_POST()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/" in msg:
            sys.stderr.write(f"[proxy] {msg}\n")


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    print(f"[proxy] Serving {PUBLIC_DIR} on :{PROXY_PORT}, proxying /api/* -> {BACKEND}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
