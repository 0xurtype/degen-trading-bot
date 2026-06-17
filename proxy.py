"""
GMGN Scanner - Reverse Proxy
Serves static files on port 3000, proxies /api/* to backend on port 8000.
"""

import http.server
import urllib.request
import os

BACKEND = "http://127.0.0.1:8000"
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy()
        else:
            self.directory = PUBLIC_DIR
            super().do_GET()

    def _proxy(self):
        try:
            url = BACKEND + self.path
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(f'{{"error":"{str(e)}"}}'.encode())

if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 3000), ProxyHandler)
    print("Proxy on :3000 → static + API proxy to :8000")
    server.serve_forever()
