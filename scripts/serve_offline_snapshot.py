"""Serve one self-contained HTML snapshot with all resource/network access denied.

This is a loopback browser test harness, not the public API service.
"""
import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'"


def serve(path, port):
    path = Path(path).resolve()
    content = path.read_bytes()
    print(json.dumps({'snapshot_sha256': hashlib.sha256(content).hexdigest(),
                      'csp': CSP, 'port': port}), flush=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/snapshot.html':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Security-Policy', CSP)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--html', type=Path, required=True)
    p.add_argument('--port', type=int, default=18013)
    a = p.parse_args()
    serve(a.html, a.port)
