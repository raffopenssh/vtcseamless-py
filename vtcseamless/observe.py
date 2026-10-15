"""The peer loop: KG → bevdirect cells → ``ne_cells build`` → epoch report → ``POST …/report``.

Rules it implements (contract doc § Input domain, § Epochs, § Observers):

1. The input domain is ``input_bbox = bbox(own parcels of the KG) + 0.004°``, rounded outward
   to 4 decimals — a pure function of the KG's own objects, NOT of the admin table's bbox
   (which is only the starting guess). The loop fetches the cells of the guess, derives the
   domain, fetches every 0.02° cell intersecting it, and repeats until the cell set is stable.
2. The build uses exactly the cells intersecting ``input_bbox`` (the same set the operator's
   baseline is built from) and passes ``input_bbox`` explicitly.
3. ``source`` is ``bevdirect@<bevdirect_version>`` read from the documents (never typed by hand);
   ``epoch`` defaults to the current ISO month.
4. The report carries digests only and is posted to the contribute prefix; the answer
   (``baseline``, ``compared``, ``chunks_changed``, ``since_last``) is stored beside the build.
5. Change protocol (``ne_cells.change``): the report also carries ``chunks_ap`` (digest of each
   chunk's rows with the register bytes zeroed); when the answer lists ``want_chunks`` the rows of
   exactly those chunks are posted to ``…/chunks`` from the build already in memory. Nothing new
   touches disk; register bytes and K rows never leave the peer.
6. QC provenance (≥ 0.3.0, bevdirect-serve ≥ v0.3.4): every report carries ``tiles{expected,
   fetched, hash}`` (the build's source-tile fetch) and ``chunks_tiles{chunk: sha256[:16]}`` (per
   res-10 chunk, over the sha256 of every tile whose bbox meets the chunk's bbox, sorted z/x/y),
   computed here from the ``tiles[]`` arrays of the cell documents — only hashes and counts leave
   the peer. ``fetched < expected`` is still posted (the server answers ``change_suspect:
   tiles_incomplete`` and asks for nothing) but logged, and the incomplete cells are re-fetched on
   the next pass (never cached). Answer keys: ``want_tiles`` is ignored, ``qc_same_input`` is a
   WARNING (our build is nondeterministic, or a cached cell was mixed with a fresh one), delta
   ``status`` may be ``pipeline`` / ``jitter`` beside ``candidate`` / ``confirmed``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from typing import Callable, Iterable, Optional

import h3

from ne_cells import canon
from ne_cells.__main__ import build_from_bevdirect
from ne_cells.change import chunk_rows, epoch_report_ap, pack_chunk_rows
from ne_cells.pack import MAGIC_LU, unpack_sections
from ne_cells.algo import INPUT_PAD_DEG, default_input_bbox

from . import profile
from .bevdirect import BevDirect, cells_for
from .public import APIError, PublicAPI, ReportResult


# ── QC provenance: tile hashes → report fields (lives here, NOT in the frozen ne_cells/) ──────

def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Web-mercator tile → (west, south, east, north) in degrees."""
    n = 2 ** z

    def lon(xx):
        return xx / n * 360.0 - 180.0

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return lon(x), lat(y + 1), lon(x + 1), lat(y)


def tile_range(z: int, w: float, s: float, e: float, n: float) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) inclusive tile index ranges at zoom z covering the lon/lat bbox."""
    nn = 2 ** z

    def tx(lon):
        return int(math.floor((lon + 180.0) / 360.0 * nn))

    def ty(lat):
        r = math.radians(max(-85.0511, min(85.0511, lat)))
        return int(math.floor((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * nn))

    return tx(w), ty(n), tx(e), ty(s)


def doc_tiles(docs: Iterable[dict]) -> Optional[list[tuple[int, int, int, str]]]:
    """Distinct (z, x, y, sha256) over the documents, sorted. None if any document predates
    bevdirect-serve v0.3.4 (no ``tiles`` array) — then no provenance is sent at all."""
    out = set()
    for d in docs:
        t = d.get("tiles")
        if not isinstance(t, list):
            return None
        for r in t:
            out.add((int(r["z"]), int(r["x"]), int(r["y"]), str(r["sha256"])))
    return sorted(out)


def tile_provenance(docs: dict, chunks: Iterable[str]) -> tuple[Optional[dict], Optional[dict]]:
    """``(tiles, chunks_tiles)`` for a report, from the cell documents of the build.

    ``tiles = {expected, fetched, hash}``: expected/fetched summed over the documents'
    ``stats.tiles_expected / tiles_fetched``, hash = sha256[:16] over all tile sha256s of the
    build, sorted. ``chunks_tiles[chunk] = sha256[:16]`` over the concatenated sha256 strings of
    the tiles whose bbox intersects the chunk's hexagon bbox, sorted by (z, x, y).
    ``(None, None)`` when a document carries no ``tiles`` (older server)."""
    tiles = doc_tiles(docs.values())
    if tiles is None:
        return None, None
    expected = fetched = 0
    for d in docs.values():
        st = d.get("stats") or {}
        expected += int(st.get("tiles_expected") or 0)
        fetched += int(st.get("tiles_fetched") or 0)
    if expected == 0:  # no counts (should not happen with tiles[] present): derive from the list
        expected = fetched = len({t[:3] for t in tiles})
    allhash = hashlib.sha256("".join(sorted(t[3] for t in tiles)).encode()).hexdigest()[:16]
    by_xyz: dict[tuple[int, int, int], list[str]] = {}
    zooms = set()
    for z, x, y, h in tiles:
        by_xyz.setdefault((z, x, y), []).append(h)
        zooms.add(z)
    chunks_tiles = {}
    for c in chunks:
        lats, lngs = zip(*h3.cell_to_boundary(c))
        w, s, e, n = min(lngs), min(lats), max(lngs), max(lats)
        parts = []
        for z in sorted(zooms):
            x0, y0, x1, y1 = tile_range(z, w, s, e, n)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    hs = by_xyz.get((z, x, y))
                    if hs:
                        parts.extend(sorted(hs))
        if parts:
            chunks_tiles[c] = hashlib.sha256("".join(parts).encode()).hexdigest()[:16]
    return dict(expected=expected, fetched=fetched, hash=allhash), chunks_tiles


def incomplete_cells(docs: dict) -> list[tuple[int, int]]:
    """Cells whose document says ``tiles_fetched < tiles_expected``."""
    out = []
    for c, d in docs.items():
        st = d.get("stats") or {}
        if int(st.get("tiles_expected") or 0) > int(st.get("tiles_fetched") or 0):
            out.append(c)
    return sorted(out)


def kg_bbox(kg: str, bev: BevDirect, api: Optional[PublicAPI] = None) -> tuple[float, float, float, float]:
    """Starting guess for the KG's extent: bevdirect-serve's embedded VGD admin row, else the
    public KG register. Only a guess — the domain is derived from the objects (see module doc)."""
    row = bev.kg(kg)
    if row:
        return row["min_lon"], row["min_lat"], row["max_lon"], row["max_lat"]
    if api:
        r = api.kg(kg)
        if r and r.get("bbox"):
            return tuple(r["bbox"])  # type: ignore[return-value]
    raise ValueError(f"KG {kg}: not in the admin table nor in the public register")


def resolve_domain(kg: str, bev: BevDirect, guess: tuple[float, float, float, float], pad: float = INPUT_PAD_DEG,
                   log: Callable[[str], None] = lambda s: None, max_rounds: int = 6):
    """Fetch cells until ``cells_for(input_bbox) ⊆ fetched``. Returns (input_bbox, cells, docs)."""
    kg = str(kg).zfill(5)
    docs: dict[tuple[int, int], dict] = {}
    need = set(cells_for(*guess))
    ibox = None
    for rnd in range(max_rounds):
        missing = sorted(need - docs.keys())
        if missing:
            log(f"round {rnd}: fetching {len(missing)} cells")
            docs.update(bev.cells(missing))
        own = []
        for d in docs.values():
            for r in d.get("parcels") or []:
                if str(r.get("kg_code") or "").zfill(5) == kg:
                    g = canon.canon_geom(r.get("geometry"))
                    if g is not None:
                        own.append(g)
        if not own:
            raise ValueError(f"KG {kg}: no own objects in {sorted(need)} — wrong code or the admin bbox is off; pass --bbox")
        ibox = default_input_bbox(own, pad)
        need = set(cells_for(*ibox))
        if need <= docs.keys():
            cells = sorted(need)
            log(f"input bbox {','.join(f'{v:.4f}' for v in ibox)} → {len(cells)} cells (x {cells[0][0]}–{cells[-1][0]})")
            return ibox, cells, {c: docs[c] for c in cells}
        log(f"round {rnd}: domain {ibox} needs {len(need - docs.keys())} more cells")
    raise RuntimeError(f"KG {kg}: domain did not converge in {max_rounds} rounds")


def observe(kg: str, bev: BevDirect, api: Optional[PublicAPI], observer: str, out_dir: str,
            epoch: Optional[str] = None, post: bool = True, bbox: Optional[tuple] = None,
            keep_inputs: bool = False, log: Callable[[str], None] = print) -> dict:
    """Build the KG's declared cells from bevdirect-serve and (optionally) report them.
    Returns a summary dict; files: ``{out_dir}/{kg}.nec``, ``{kg}.build.json``, ``{kg}.report.json``."""
    kg = str(kg).zfill(5)
    t0 = time.time()
    os.makedirs(out_dir, exist_ok=True)
    epoch = epoch or time.strftime("%Y-%m", time.gmtime())
    version = bev.version
    if version != profile.BEVDIRECT_TAG:
        log(f"note: bevdirect-serve is {version}, the pinned baseline tag is {profile.BEVDIRECT_TAG} — "
            f"source bevdirect@{version} is only compared with reports of the same tag")
    guess = tuple(bbox) if bbox else kg_bbox(kg, bev, api)
    ibox, cells, docs = resolve_domain(kg, bev, guess, log=log)

    inputs = os.path.join(out_dir, f"{kg}.inputs")
    os.makedirs(inputs, exist_ok=True)
    paths = []
    for (ix, iy), d in docs.items():
        p = os.path.join(inputs, f"cell_{ix}_{iy}.json")
        with open(p, "w") as f:
            json.dump(d, f, separators=(",", ":"), ensure_ascii=False)
        paths.append(p)
    nec = os.path.join(out_dir, f"{kg}.nec")
    built = build_from_bevdirect(kg, epoch, paths, nec, input_bbox=ibox)
    built["cells_fetched"] = [list(c) for c in cells]
    built["bevdirect"] = dict(url=bev.url, version=version, requests=bev.stats.requests, cache_hits=bev.stats.cache_hits,
                              pending_rounds=bev.stats.pending_rounds, fetch_seconds=round(bev.stats.seconds, 1))
    with open(os.path.join(out_dir, f"{kg}.build.json"), "w") as f:
        json.dump(built, f, indent=1)
    if not keep_inputs:
        for p in paths:
            os.remove(p)
        os.rmdir(inputs)
    log(f"built {kg}: {built['cells']} cells / {built['kcells']} K, digest {built['digest']}, source {built['source']}, "
        f"{built['seconds']} s build")

    summary = dict(kg=kg, digest=built["digest"], source=built["source"], epoch=epoch, cells=built["cells"],
                   kcells=built["kcells"], input_bbox=list(ibox), cells_fetched=len(cells), nec=nec,
                   seconds=round(time.time() - t0, 1), posted=False)
    with open(nec, "rb") as f:
        sec = [s for s in unpack_sections(f.read()) if s.magic == MAGIC_LU][0]
    rep = epoch_report_ap(sec, observer)
    tiles, chunks_tiles = tile_provenance(docs, rep.get("chunks") or {})
    if tiles is not None:
        rep["tiles"], rep["chunks_tiles"] = tiles, chunks_tiles
        summary["tiles"] = tiles
        if tiles["fetched"] < tiles["expected"]:
            bad = incomplete_cells(docs)
            summary["tiles_incomplete_cells"] = [list(c) for c in bad]
            log(f"WARNING kg {kg}: incomplete tile fetch ({tiles['fetched']}/{tiles['expected']} tiles) in cells "
                f"{bad} — reported as is (server: change_suspect tiles_incomplete), cells re-fetched on the next pass")
    else:
        log(f"note kg {kg}: documents carry no tiles[] (bevdirect-serve < v0.3.4) — no QC provenance sent")
    with open(os.path.join(out_dir, f"{kg}.report.json"), "w") as f:
        json.dump(rep, f, separators=(",", ":"))
    summary["chunks"] = len(rep.get("chunks") or {})
    if post:
        if api is None:
            raise ValueError("post=True needs a PublicAPI")
        res: ReportResult = api.report(rep)
        with open(os.path.join(out_dir, f"{kg}.report.json"), "w") as f:
            json.dump(dict(report=rep, answer=res.body, posted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())), f,
                      separators=(",", ":"))
        summary.update(posted=True, answer=res.body, baseline=res.baseline, compared=res.compared, unchanged=res.unchanged,
                       chunks_same=res.chunks_same, chunks_changed=len(res.chunks_changed),
                       since_last_changed=len(res.since_last.get("changed") or []), want_chunks=len(res.want_chunks))
        log(res.summary())
        if res.qc_same_input:
            summary["qc_same_input"] = res.qc_same_input
            log(f"WARNING kg {kg}: qc_same_input={res.qc_same_input} — {res.qc_same_input} chunk(s) changed digest while their "
                f"source tiles did not: our build is nondeterministic or a cached cell was mixed with a fresh one")
        if res.change_suspect:
            summary["change_suspect"] = res.change_suspect
        if res.want_chunks:
            # step 2: the rows of the wanted chunks, from the section already in memory (no file, no inputs)
            rows = chunk_rows(sec.raw, sec.header["cells_n"], res.want_chunks)
            tot = dict(stored=0, seen=0, rejected=0, deltas=0)
            by_status: dict[str, int] = {}
            for part in _split_bodies(rows, CHUNKS_BODY_MAX):
                ans = api.chunks(kg, observer, pack_chunk_rows(part))
                body = ans.data if isinstance(ans.data, dict) else {}
                for k in ("stored", "seen", "rejected"):
                    tot[k] += int(body.get(k) or 0)
                for d in body.get("deltas") or []:
                    tot["deltas"] += 1
                    st = str(d.get("status") or "candidate")  # candidate | confirmed | pipeline | jitter
                    by_status[st] = by_status.get(st, 0) + 1
            summary.update(chunks_posted=len(rows), chunks_stored=tot["stored"], chunks_rejected=tot["rejected"], deltas=tot["deltas"],
                           deltas_by_status=by_status)
            log(f"kg {kg}: posted {len(rows)} chunk rows → stored={tot['stored']} seen={tot['seen']} "
                f"rejected={tot['rejected']} deltas={tot['deltas']} {by_status}")
            if by_status.get("pipeline"):
                log(f"WARNING kg {kg}: {by_status['pipeline']} delta(s) with status pipeline — same source tiles, different rows; "
                    f"not counted as change by the server")
    return summary


CHUNKS_BODY_MAX = 1_500_000   # server limit is 2 MB per POST …/chunks


def _split_bodies(rows: dict, limit: int) -> list[dict]:
    """Chunk-row dicts whose packed size stays under `limit` (order = chunk id)."""
    out, cur, size = [], {}, 6
    for c in sorted(rows):
        n = 12 + len(rows[c])
        if cur and size + n > limit:
            out.append(cur)
            cur, size = {}, 6
        cur[c] = rows[c]
        size += n
    if cur:
        out.append(cur)
    return out


def observe_many(kgs: Iterable[str], **kw) -> list[dict]:
    out = []
    for kg in kgs:
        try:
            out.append(observe(kg, **kw))
        except (APIError, ValueError, RuntimeError) as e:  # one bad KG must not stop a pass
            kw.get("log", print)(f"FAIL {kg}: {e}")
            out.append(dict(kg=str(kg).zfill(5), error=str(e)))
    return out
