"""
GMGN Scanner - Reverse Proxy
Serves static files on port 3000, proxies /api/* to backend on port 8000.
"""

import http.server
import urllib.request
import os
import mimetypes

BACKEND = "http://127.0.0.1:8000"
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy()
        else:
            self._serve_static()

    def _serve_static(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        filepath = os.path.join(PUBLIC_DIR, path.lstrip("/"))
        filepath = os.path.normpath(filepath)
        if not filepath.startswith(PUBLIC_DIR):
            self.send_error(403)
            return
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(filepath)
        if not mime:
            mime = "application/octet-stream"
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def _proxy(self):
        try:
            url = BACKEND + self.path
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            err = f'{{"error":"{str(e)}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 3000), ProxyHandler)
    print("Proxy on :3000 → static + API proxy to :8000", flush=True)
    server.serve_forever()
