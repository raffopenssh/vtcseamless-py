"""ne_cells determinism + contract tests on synthetic input (no real data in the repo)."""
import json
import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import synth  # noqa: E402
from ne_cells import canon, algo  # noqa: E402
from ne_cells.algo import build_cells, K, ALGO, H3_RES, K_RES  # noqa: E402
from ne_cells.pack import (MAGIC_LU, MAGIC_OBS, lu_header, pack_records, pack_section, unpack_sections,  # noqa: E402
                           LU_REC, K_REC, epoch_report)

KG = synth.KG_WEST


def _inputs(seed=None, big=True):
    P, F, L = synth.objects(big=big)
    if seed is not None:
        rnd = random.Random(seed)
        rnd.shuffle(P); rnd.shuffle(F); rnd.shuffle(L)
        F = F + F[: len(F) // 3]          # tile copies, as a tile fetch delivers them
        L = L + L[: len(L) // 2]
    return (canon.parcels_from_index([dict(p, status=p["rstatus"]) for p in P]),
            canon.footprints_from_index(F), canon.landuse_from_index([dict(l, code=l["ns"]) for l in L]))


def _blob(seed=None):
    P, F, L = _inputs(seed)
    r = build_cells(KG, P, F, L)
    raw = pack_records(r.cells, r.kcells)
    hdr = lu_header(KG, "2026-10", "test", ALGO, H3_RES, K_RES, K, len(r.cells), len(r.kcells), r.stats, input_bbox=r.input_bbox)
    return pack_section(MAGIC_LU, hdr, raw), r


def test_bit_identical_under_input_order_and_duplicates():
    a, ra = _blob(None)
    b, rb = _blob(7)
    c, _ = _blob(99)
    da, db, dc = (unpack_sections(x)[0].header["digest"] for x in (a, b, c))
    assert da == db == dc and ra.cells and ra.kcells is not None
    assert unpack_sections(a)[0].raw == unpack_sections(b)[0].raw


def test_roundtrip_and_header():
    blob, r = _blob()
    sec = unpack_sections(blob)[0]
    h = sec.header
    assert sec.magic == MAGIC_LU and h["algo"] == ALGO and h["kg"] == KG and h["h3_res"] == 12 and h["k_res"] == 10
    assert h["cells_n"] == len(r.cells) and len(sec.raw) == h["cells_n"] * LU_REC.size + h["kcells_n"] * K_REC.size
    cells, kcells = sec.records()
    assert len(cells) == h["cells_n"] and all(sum(v["lu"]) <= 255 + 9 for v in cells.values())
    assert h["coord_decimals"] == 7 and h["input_bbox"] and h["attribution"].startswith("Datenquelle: BEV")


def test_bevdirect_adapter_same_result():
    """The same village delivered as two bevdirect cell documents (crossing copies complete:false
    in one of them) yields the same record block as the whole-village rows."""
    P, F, L = _inputs(big=False)
    whole = build_cells(KG, P, F, L)
    docs = [synth.cell_document(ix, iy, big=False) for ix, iy in synth.village_cells()]
    Pb, Fb, Lb = canon.from_bevdirect(docs)
    assert len(Pb) > len(P), "objects straddling the cell edge must appear in both documents"
    split = build_cells(KG, Pb, Fb, Lb, input_bbox=whole.input_bbox)
    assert pack_records(split.cells, split.kcells) == pack_records(whole.cells, whole.kcells)
    assert canon.bevdirect_source(docs) == "bevdirect@" + synth.VERSION
    # objects wider than the pad arrive as truncated copies; the build must still be deterministic
    docs = [synth.cell_document(ix, iy) for ix, iy in synth.village_cells()]
    Pb, Fb, Lb = canon.from_bevdirect(docs)
    assert any(not p["complete"] for p in Pb)
    a = build_cells(synth.KG_EAST, Pb, Fb, Lb)
    b = build_cells(synth.KG_EAST, list(reversed(Pb)), Fb[::-1], Lb[::-1])
    assert pack_records(a.cells, a.kcells) == pack_records(b.cells, b.kcells) and a.cells


def test_obs_section_passthrough():
    rows = b"".join(struct.pack("<Q", i) + bytes(33) for i in range(10))
    hdr = dict(magic="NEO1", fmt_ver=1, algo="obs-cells-1", epoch="2026-10", source="peer@1", kg=KG, h3_res=12,
               cells_n=10, record_bytes=41, groups=list(algo.GROUP_ORDER) if hasattr(algo, "GROUP_ORDER") else None)
    sec = unpack_sections(pack_section(MAGIC_OBS, hdr, rows))[0]
    assert sec.magic == MAGIC_OBS and sec.raw == rows and sec.header["record_bytes"] == 41


def test_epoch_report_shape():
    blob, r = _blob()
    sec = unpack_sections(blob)[0]
    rep = epoch_report(sec, "peer-1")
    assert rep["kg"] == KG and rep["digest"] == sec.header["digest"] and rep["chunk_res"] == 10 and rep["chunks"]
    assert all(len(v) == 16 for v in rep["chunks"].values()) and rep["chunks_total"] >= len(rep["chunks"])
    json.dumps(rep)
