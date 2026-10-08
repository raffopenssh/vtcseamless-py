import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import synth  # noqa: E402
from vtcseamless.bevdirect import BevDirect, PendingError, cell_bbox  # noqa: E402


class FakeServe(BaseHTTPRequestHandler):
    """Minimal bevdirect-serve: /health, /viewport (first call per cell pending), /kg/{kg}."""
    pending_once = True
    null_footprints = False
    hits = {}

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Data-Attribution", "(c) BEV, 2026 - test")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            return self._json(200, {"ok": True, "bevdirect_version": synth.VERSION, "admin_source": "test"})
        if u.path == "/viewport":
            w, s = float(q["west"]), float(q["south"])
            if float(q["east"]) - w > 0.045 + 1e-9:
                return self._json(400, {"error": "bbox too large"})
            ix, iy = round(w / 0.02), round(s / 0.02)
            key = (ix, iy)
            FakeServe.hits[key] = FakeServe.hits.get(key, 0) + 1
            if FakeServe.pending_once and FakeServe.hits[key] == 1 and float(q.get("wait", "20")) < 5:
                return self._json(200, {"parcels": [], "ready": False, "pending": True, "retry_after_s": 0})
            doc = synth.cell_document(ix, iy)
            if FakeServe.null_footprints:
                doc = dict(doc, footprints=None)  # bevdirect-serve < v0.3.3 on a cell without buildings
            return self._json(200, doc)
        if u.path.startswith("/kg/"):
            return self._json(200, {"kg": {"kg_code": u.path[4:], "min_lon": synth.ORIGIN_LON, "min_lat": synth.ORIGIN_LAT,
                                           "max_lon": synth.ORIGIN_LON + synth.N * synth.STEP, "max_lat": synth.ORIGIN_LAT + synth.N * synth.STEP}})
        self._json(404, {"error": "nope"})


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeServe)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_viewport_and_cell_cache(server, tmp_path):
    bev = BevDirect(server, cache_dir=str(tmp_path), doc_ttl_s=3600)
    assert bev.version == synth.VERSION
    (ix, iy) = synth.village_cells()[0]
    d = bev.cell(ix, iy)
    assert d["ready"] and d["parcels"] and d["bevdirect_version"] == synth.VERSION
    assert (tmp_path / "cells" / f"{ix}_{iy}.json.gz").exists()
    assert (tmp_path / "ATTRIBUTION.txt").read_text().startswith("© BEV")
    n = bev.stats.requests
    d2 = bev.cell(ix, iy)
    assert d2 == d and bev.stats.requests == n and bev.stats.cache_hits == 1
    # TTL: an old file is ignored and removed
    p = tmp_path / "cells" / f"{ix}_{iy}.json.gz"
    os.utime(p, (time.time() - 4000, time.time() - 4000))
    bev.cell(ix, iy)
    assert bev.stats.requests == n + 1


def test_pending_then_ready(server):
    FakeServe.hits.clear()
    bev = BevDirect(server, deadline_s=10)
    ix, iy = synth.village_cells()[1]
    # wait= is clamped to the remaining deadline; the fake answers pending to the first short wait
    bev.deadline_s = 2
    try:
        bev.cell(ix, iy)
    except PendingError as e:
        assert e.document["pending"] is True
    bev.deadline_s = 30
    assert bev.cell(ix, iy)["ready"]


def test_bbox_validation(server):
    bev = BevDirect(server)
    with pytest.raises(ValueError):
        bev.viewport(15.0, 47.0, 15.1, 47.01)
    with pytest.raises(ValueError):
        bev.viewport(15.0, 47.0, 14.9, 47.01)


def test_cell_bbox_is_server_grid():
    assert cell_bbox(755, 2356) == (15.1, 47.12, 15.12, 47.14)


def test_null_layer_is_normalised(server, tmp_path):
    """bevdirect-serve < v0.3.3 emits null for an empty layer; the frozen ne_cells.canon
    iterates every layer, so the client must hand it [] (alpine cells have no footprints)."""
    FakeServe.pending_once, FakeServe.null_footprints = False, True
    try:
        bev = BevDirect(server, cache_dir=str(tmp_path / "c"))
        doc = bev.cell(9000, 2000)
        assert doc["footprints"] == []
        from ne_cells import canon
        canon.from_bevdirect([doc])  # must not raise
    finally:
        FakeServe.null_footprints = False
