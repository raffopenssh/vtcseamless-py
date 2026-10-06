"""A fake of the public overlay API + contribute prefix for the tests (no network)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TOKEN = "test-contributor-token"


class FakePublic(BaseHTTPRequestHandler):
    reports = []          # bodies received
    rate_limit_once = True
    manifest_hits = 0

    def log_message(self, *a):
        pass

    def _json(self, code, body, extra=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Data-Attribution", "(c) BEV (CC BY 4.0), bearbeitet - test")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/v1/ne":
            return self._json(200, {"frozen": {"algo": "ne-cells-2"}, "kgs_built": 1, "chunks": 10})
        if u.path == "/api/v1/ne/manifest":
            FakePublic.manifest_hits += 1
            if self.headers.get("If-None-Match") == '"nem-1"':
                self.send_response(304); self.end_headers(); return
            body = {"generated_at": "2026-10-06T00:00:00Z", "kgs": {"99901": {"status": "built", "lu": {"digest": "abc"}}}, "delta": bool(q.get("since"))}
            return self._json(200, body, {"ETag": '"nem-1"'})
        if u.path == "/api/v1/kgs":
            return self._json(200, {"data": {"kg_count": 1, "kgs": [{"kg_code": "99901", "kg_name": "Test", "bbox": [15.11, 47.13, 15.13, 47.15]}]}})
        if u.path == "/api/v1/ne/cells":
            return self._json(200, {"ids": ["8c1e108cc922dff"], "lu": {"land": [255]}})
        if u.path.startswith("/contrib/api/v1/ne/") and u.path.endswith("/report"):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"reports": FakePublic.reports})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path.startswith("/contrib/api/v1/ne/") and u.path.endswith("/report"):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._json(404, {"error": "not found"})
            if FakePublic.rate_limit_once:
                FakePublic.rate_limit_once = False
                return self._json(429, {"error": "rate limit"}, {"Retry-After": "1"})
            for k in ("kg", "algo", "epoch", "source", "observer", "digest", "chunks"):
                if k not in body:
                    return self._json(400, {"error": f"missing {k}"})
            FakePublic.reports.append(body)
            chunks = body["chunks"]
            return self._json(200, {"kg": body["kg"], "baseline": "this_report" if len(FakePublic.reports) == 1 else "report",
                                    "compared": len(FakePublic.reports) > 1, "chunks_same": list(chunks) if len(FakePublic.reports) > 1 else [],
                                    "chunks_changed": [], "chunks_unknown_to_us": [], "chunks_baselined_now": len(chunks) if len(FakePublic.reports) == 1 else 0,
                                    "since_last": {"compared": len(chunks), "same": len(chunks), "changed": []}, "token": "test"})
        self._json(404, {"error": "not found"})


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakePublic)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"
