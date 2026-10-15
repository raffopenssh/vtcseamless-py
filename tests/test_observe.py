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
from vtcseamless.observe import observe, resolve_domain, _split_bodies  # noqa: E402


def test_split_bodies():
    rows = {format(i, "x"): bytes(30 * 10) for i in range(100)}
    parts = _split_bodies(rows, 1000)
    assert sum(len(p) for p in parts) == 100 and all(6 + sum(12 + len(b) for b in p.values()) <= 1000 for p in parts) and len(parts) > 1
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
    # change protocol: chunks_ap travelled with the report, the wanted rows went out as NECH with the
    # register bytes zeroed, and NO new file appeared (only .nec/.build.json/.report.json on disk)
    assert set(rep["report"]["chunks_ap"]) == set(rep["report"]["chunks"]) and s1["want_chunks"] == s1["chunks"]
    assert s1["chunks_posted"] == s1["chunks"] and len(fake_public.FakePublic.chunk_bodies) == 1
    body = fake_public.FakePublic.chunk_bodies[0]
    assert body[:4] == b"NECH" and True  # layout checked below
    off, nch = 6, int.from_bytes(body[4:6], "little")
    for _ in range(nch):
        ln = int.from_bytes(body[off + 8:off + 12], "little"); rows = body[off + 12:off + 12 + ln]; off += 12 + ln
        assert ln % 30 == 0 and all(rows[i + 24] == 0 and rows[i + 25] == 0 for i in range(0, ln, 30))
    assert sorted(os.listdir(out)) == ["99902.build.json", "99902.nec", "99902.report.json"]
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


def _fresh(tmp_path, burl, purl):
    fake_public.FakePublic.reports.clear()
    fake_public.FakePublic.chunk_bodies.clear()
    fake_public.FakePublic.rate_limit_once = False
    fake_public.FakePublic.qc_same_input = 0
    fake_public.FakePublic.delta_status = "candidate"
    FakeServe.drop_tiles = {}
    FakeServe.no_tiles = False
    return BevDirect(burl, cache_dir=str(tmp_path / "cache")), PublicAPI(purl, token=fake_public.TOKEN, retry_s=10), str(tmp_path / "out")


def test_report_carries_tile_provenance_stable(servers, tmp_path):
    """tiles{} and chunks_tiles{} are in the posted report, stable across two runs (one fresh, one
    from the document cache), and computed only from the documents' tiles[] (no ne_cells change)."""
    burl, purl = servers
    bev, api, out = _fresh(tmp_path, burl, purl)
    logs = []
    s1 = observe(synth.KG_EAST, bev, api, "test-peer", out, epoch="2026-10", log=logs.append)
    rep1 = fake_public.FakePublic.reports[0]
    t = rep1["tiles"]
    assert set(t) == {"expected", "fetched", "hash"} and t["expected"] == t["fetched"] > 0 and len(t["hash"]) == 16
    ct = rep1["chunks_tiles"]
    assert set(ct) == set(rep1["chunks"]) and all(len(v) == 16 and int(v, 16) >= 0 for v in ct.values())
    # the first-pass answer carried want_tiles (ignored) — the loop still posted every wanted chunk
    assert rep1["chunks_ap"] and s1["chunks_posted"] == s1["chunks"] and "want_tiles" in s1["answer"]
    assert s1["deltas_by_status"] == {"candidate": s1["deltas"]} and not any("WARNING" in m for m in logs)
    # second run: documents from the cache → identical provenance
    bev2 = BevDirect(burl, cache_dir=str(tmp_path / "cache"))
    s2 = observe(synth.KG_EAST, bev2, api, "test-peer", out, epoch="2026-10", log=lambda m: None)
    rep2 = fake_public.FakePublic.reports[1]
    assert bev2.stats.cache_hits == 2 and rep2["tiles"] == t and rep2["chunks_tiles"] == ct and s2["digest"] == s1["digest"]
    # the hashes are over tile sha256s only: a different tile set gives a different hash (no object data involved)
    from vtcseamless.observe import tile_provenance
    docs = {c: synth.cell_document(*c) for c in synth.village_cells()}
    docs2 = {c: dict(d, tiles=synth.cell_tiles(*c, seed=2)[0]) for c, d in docs.items()}  # same objects, other tile bytes
    ta, ca = tile_provenance(docs, rep1["chunks"]); tb, cb = tile_provenance(docs2, rep1["chunks"])
    assert ta == t and ca == ct and tb["hash"] != t["hash"] and all(cb[c] != ca[c] for c in ca)
    # only the tiles that cover a chunk feed its hash: changing one tile moves a subset of chunks
    west = synth.village_cells()[0]
    partial = False
    for i, tile in enumerate(docs[west]["tiles"]):
        docs3 = {c: json.loads(json.dumps(d)) for c, d in docs.items()}
        docs3[west]["tiles"][i]["sha256"] = "0" * 64
        _, cc = tile_provenance(docs3, rep1["chunks"])
        moved = [c for c in ca if cc[c] != ca[c]]
        assert len(moved) < len(ca), tile
        partial = partial or 0 < len(moved)
    assert partial


def test_incomplete_fetch_is_reported_and_refetched(servers, tmp_path):
    burl, purl = servers
    bev, api, out = _fresh(tmp_path, burl, purl)
    west = synth.village_cells()[0]
    FakeServe.drop_tiles = {west: 2}
    logs = []
    s1 = observe(synth.KG_EAST, bev, api, "test-peer", out, epoch="2026-10", log=logs.append)
    rep = fake_public.FakePublic.reports[0]
    assert rep["tiles"]["fetched"] == rep["tiles"]["expected"] - 2 and s1["tiles_incomplete_cells"] == [list(west)]
    assert s1["answer"]["change_suspect"] == "tiles_incomplete" and s1["want_chunks"] == 0 and "chunks_posted" not in s1
    assert any("WARNING" in m and "incomplete tile fetch" in m for m in logs)
    # the incomplete cell was NOT cached: the next pass re-fetches it (the complete one is served from cache)
    FakeServe.drop_tiles = {}
    bev2 = BevDirect(burl, cache_dir=str(tmp_path / "cache"))
    s2 = observe(synth.KG_EAST, bev2, api, "test-peer", out, epoch="2026-10", log=lambda m: None)
    assert bev2.stats.cache_hits == 1 and bev2.stats.requests >= 2 and "tiles_incomplete_cells" not in s2
    assert fake_public.FakePublic.reports[1]["tiles"]["fetched"] == fake_public.FakePublic.reports[1]["tiles"]["expected"]


def test_qc_answer_keys_are_logged(servers, tmp_path):
    burl, purl = servers
    bev, api, out = _fresh(tmp_path, burl, purl)
    fake_public.FakePublic.qc_same_input = 3
    fake_public.FakePublic.delta_status = "pipeline"
    logs = []
    s1 = observe(synth.KG_EAST, bev, api, "test-peer", out, epoch="2026-10", log=logs.append)
    assert s1["qc_same_input"] == 3 and s1["deltas_by_status"] == {"pipeline": s1["deltas"]} and s1["deltas"] > 0
    assert any(m.startswith("WARNING kg 99902: qc_same_input=3") for m in logs)
    assert any("status pipeline" in m for m in logs)


def test_old_server_without_tiles_sends_no_provenance(servers, tmp_path):
    burl, purl = servers
    bev, api, out = _fresh(tmp_path, burl, purl)
    FakeServe.no_tiles = True
    try:
        logs = []
        observe(synth.KG_EAST, bev, api, "test-peer", out, epoch="2026-10", log=logs.append)
        rep = fake_public.FakePublic.reports[0]
        assert "tiles" not in rep and "chunks_tiles" not in rep and any("no tiles[]" in m for m in logs)
    finally:
        FakeServe.no_tiles = False
