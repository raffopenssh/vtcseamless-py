"""End to end on synthetic data: fake bevdirect-serve + fake public API → observe() → .nec,
report posted, summary; a second run is bit-identical and compares against the first."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fake_public  # noqa: E402
import synth  # noqa: E402
from test_bevdirect_client import FakeServe  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
import threading  # noqa: E402

from ne_cells.pack import unpack_sections  # noqa: E402
from vtcseamless.bevdirect import BevDirect  # noqa: E402
from vtcseamless.observe import observe, resolve_domain  # noqa: E402
from vtcseamless.public import PublicAPI  # noqa: E402


@pytest.fixture(scope="module")
def servers():
    FakeServe.pending_once = False
    bs = ThreadingHTTPServer(("127.0.0.1", 0), FakeServe)
    threading.Thread(target=bs.serve_forever, daemon=True).start()
    ps, purl = fake_public.start()
    yield f"http://127.0.0.1:{bs.server_port}", purl
    bs.shutdown(); ps.shutdown()


def test_domain_from_objects_not_from_guess(servers):
    burl, _ = servers
    bev = BevDirect(burl)
    # a deliberately wrong guess (one cell east of the village) still converges on the village
    ibox, cells, docs = resolve_domain(synth.KG_WEST, bev, (15.1251, 47.131, 15.126, 47.132))
    assert set(cells) == set(synth.village_cells())
    w, s, e, n = ibox
    assert w <= synth.ORIGIN_LON - 0.004 + 1e-9 and e >= synth.ORIGIN_LON + synth.N // 2 * synth.STEP + 0.004 - 1e-9


def test_observe_end_to_end(servers, tmp_path):
    burl, purl = servers
    fake_public.FakePublic.reports.clear()
    fake_public.FakePublic.rate_limit_once = False
    bev = BevDirect(burl, cache_dir=str(tmp_path / "cache"))
    api = PublicAPI(purl, token=fake_public.TOKEN, retry_s=10)
    out = str(tmp_path / "out")
    s1 = observe(synth.KG_EAST, bev, api, "test-peer", out, epoch="2026-10", log=lambda s: None)
    assert s1["posted"] and s1["source"] == "bevdirect@" + synth.VERSION and s1["cells"] > 0 and s1["chunks"] > 0
    assert s1["baseline"] == "this_report" and s1["cells_fetched"] == 2
    sec = unpack_sections(open(os.path.join(out, "99902.nec"), "rb").read())[0]
    assert sec.header["kg"] == "99902" and sec.header["digest"] == s1["digest"] and sec.header["inputs"][0]["bevdirect_version"] == synth.VERSION
    rep = json.load(open(os.path.join(out, "99902.report.json")))
    assert rep["answer"]["baseline"] == "this_report" and "chunks" in rep["report"] and rep["report"]["cells_n"] == s1["cells"]
    assert not os.path.exists(os.path.join(out, "99902.inputs"))
    # second pass: served from the document cache, same digest, compared against the first report
    bev2 = BevDirect(burl, cache_dir=str(tmp_path / "cache"))
    s2 = observe(synth.KG_EAST, bev2, api, "test-peer", out, epoch="2026-10", log=lambda s: None)
    assert s2["digest"] == s1["digest"] and bev2.stats.cache_hits == 2 and bev2.stats.requests <= 2
    assert s2["compared"] and s2["chunks_same"] == s1["chunks"] and s2["chunks_changed"] == 0


def test_observe_refuses_unknown_kg(servers, tmp_path):
    burl, purl = servers
    bev = BevDirect(burl)
    with pytest.raises(ValueError):
        observe("99999", bev, PublicAPI(purl, token=fake_public.TOKEN, retry_s=5), "t", str(tmp_path), post=False, log=lambda s: None)
