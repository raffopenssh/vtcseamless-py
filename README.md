# vtcseamless-py — the Python side of vtcseamless / bevdirect

[vtcseamless](https://github.com/raffopenssh/vtcseamless) (Go, MIT) turns the BEV vector
tiles (VTC, CC BY 4.0) into seamless polygons; its preset **bevdirect-serve** is a one-binary
service that assembles parcels, building footprints and land use for any viewport, straight
from the tiles, with a 24 h tile cache and a 6 h cell cache. **This package does not port that
engine** — the assembly (own-tile clip, guarded union, seam merge) is part of every downstream
digest, and two implementations would never be bit-identical. It gives a Python peer everything
*around* the engine:

| module | what |
|---|---|
| `vtcseamless.bevdirect` | client for bevdirect-serve on its 0.02° cell grid (`wait=0` retry loop, 24 h document cache); `install()` / `Server` download the release binary and run it as a child process — no Go, no root, no credentials |
| `ne_cells` | the **frozen reference implementation** of the NE cell statistics (algo `ne-cells-2`, 30-byte res-12 rows, 29-byte res-10 K rows, `NEC1` container, epoch reports). Pinned `shapely==2.1.2`, `h3==4.5.0` |
| `vtcseamless.public` | the public overlay API: NE manifest / chunks / cells, the KG register, `/context`; and the contribute endpoint for epoch reports (token) |
| `vtcseamless.observe` | the peer loop: KG → cells → `ne_cells build` → report → answer |
| `vtcseamless.cache` | licence-compliant response cache (notice + date stored with every entry, ETag revalidation, pending answers never cached) |

Hosts, the bevdirect tag pin and the attribution text live in **one file**,
`vtcseamless/profile.py` (overridable by `BEVDIRECT_URL`, `VTC_PUBLIC_API`, `VTC_TOKEN`);
a test fails if they appear anywhere else.

```
pip install git+https://github.com/raffopenssh/vtcseamless-py      # python ≥ 3.11
vtcseamless serve &                       # downloads bevdirect-serve v0.3.3 once, runs it on :8787
vtcseamless health
vtcseamless observe --kg <kg> --observer <my-label> --token $VTC_TOKEN     # → ./ne_out/<kg>.nec + report answer
```

**Aligned cells only.** Every build this package makes is from aligned 0.02° cell documents
(`ix=floor(lon/0.02)`), never from a free viewport: bevdirect-serve's multi-cell `/viewport`
keeps one truncated copy of a parcel wider than cell + pad, so its digests differ from the
operator's by hundreds of chunks (75110/73008, Oct 2026) and `/report` answers
`change_suspect: coverage_lossy` for such builds (cells_n below the best build seen for the
bbox). Peers with their own pipeline: fetch cells, not viewports. bevdirect-serve < v0.3.3
emits `null` for an empty layer; this client normalises it to `[]` (≥ 0.1.1).

## The peer loop (`vtcseamless observe`)

1. **Domain.** `input_bbox = bbox(own parcels of the KG) + 0.004°`, rounded outward to 4
   decimals — a function of the objects, not of any admin table. The loop starts from the
   server's embedded VGD bbox (or the public KG register), fetches those cells, derives the
   domain, fetches every 0.02° cell intersecting it, and repeats until the cell set is stable.
2. **Cells.** Each cell is `GET /viewport` for the *aligned* cell box (`ix = floor(lon/0.02)`),
   the same unit the operator baselines are built from. Documents are cached 24 h as
   `~/.cache/vtcseamless/cells/{ix}_{iy}.json.gz` (only `ready:true` ones; `ATTRIBUTION.txt`
   beside them). Pending answers are retried with `retry_after_s`; a cell that stays pending
   past the deadline raises — it is never treated as empty.
3. **Build.** `ne_cells.build_from_bevdirect(kg, epoch, cells, input_bbox=…)`; `source` is
   `bevdirect@<bevdirect_version>` read from the documents, `epoch` the current ISO month.
4. **Report.** Digests only (whole build + one per res-10 chunk whose every res-12 child lies
   inside the coverage bbox) → `POST {CONTRIB_PREFIX}/api/v1/ne/{kg}/report`. The answer
   (`baseline`, `compared`, `chunks_same / chunks_changed / chunks_unknown_to_us`,
   `since_last`) is stored as `{kg}.report.json`. 2 POST/s per token, burst 30; a 429 is
   waited out per `Retry-After`, never retried early.

5. **Own declared layer (optional).** `{kg}.nec` is a complete NEC1 section; the container
   format is self-delimiting, so a peer that also computes an observed layer (NEO1) can hand
   the operator `nec_bytes + neo1_bytes` in one body — the operator then validates the
   observed cell set against *this* build (its `bev` section) instead of the frozen index
   epoch, which differs by 1–3 border cells on about half the KGs. That write path is
   operator-private and not part of this package; only the `.nec` output is.

Verified against the operator: one rural KG, 20 cells, 52 478 res-12 cells / 649 K rows /
1163 chunks, built from a local bevdirect-serve `v0.3.0` by this loop → digest **identical to
the operator's `bev` baseline** (`identical_kg_digest:true`, 1163/1163 chunks same). Timing on
2 cores: 20 cells 22 s (16 warm, 4 cold), build 60 s, report < 1 s.

Python API:

```python
from vtcseamless import BevDirect, PublicAPI, observe
bev = BevDirect("http://127.0.0.1:8787", cache_dir="~/.cache/vtcseamless")
api = PublicAPI(token="…")                      # base = profile.PUBLIC_API
summary = observe("<kg>", bev, api, observer="peer-3", out_dir="./ne_out")
summary["digest"], summary["compared"], summary["chunks_changed"]
```

## bevdirect-serve from Python

```python
from vtcseamless import Server, BevDirect, cells_for
with Server("~/.local/share/vtcseamless", port=8787) as srv:   # install() on first use
    bev = BevDirect(srv.url)
    doc = bev.viewport(W, S, E, N)              # blocks until ready:true (deadline 300 s)
    for ix, iy in cells_for(W, S, E, N):
        cell = bev.cell(ix, iy)                 # one aligned cell, 24 h cache
    bev.parcel("<kg>-<gnr>", lon, lat)          # resolved from the tiles around the point
    bev.ez("<kg>", "<ez>", lon, lat)            # the folio's objects inside the window only
```

For a system service use the Go repo's `bootstrap.sh` (systemd unit, same flags). The
server's `/viewport` document is the contract: `parcels[] {parcel_id, kg_code, gnr, ez,
rstatus, area_sqm, lon, lat, complete, geometry, dominant_ns, landuse_areas, building_count}`,
`footprints[]`, `landuse[]`, `ready`, `bevdirect_version`, `coord_decimals: 7`, `notice`.
`complete:false` = truncated at the assembly pad; `ready:false, pending:true` = unknown.

## Public overlay API

```python
api = PublicAPI()                                # tokenless reads
api.ne()                                         # frozen pin, coverage, dictionary ids
m = api.ne_manifest(); api.ne_manifest(since=m["generated_at"])   # delta polling
api.ne_cells(lon=…, lat=…, k=1).data             # the res-12 cell under a point + ring
api.ne_cells(bbox=(W, S, E, N), list_only=True)  # which res-9 chunks cover a viewport
api.ne_chunk("<h3 res 9>", fmt="zstd")           # the primitive, immutable per digest
api.kgs(); api.kg("<kg>")                        # the KG register (7 850 rows, bbox, names)
api.context(lon, lat, include="ne")              # one-call overlay bundle for a point
```

Every answer keeps `X-Data-Attribution` (`Answer.notice`); render it with the data.
`vtcseamless.cache.LicensedCache` adds the persistent, dated, attributed cache with ETag
revalidation (24 h revalidate, 7 d discard; pending/non-200 never stored).

## ne_cells (reference, frozen)

```
ne_cells build  --kg <kg> --epoch 2026-10 --bevdirect cell_a.json --bevdirect cell_b.json … -o out.nec
ne_cells report out.nec --observer <label>      # → JSON body for the contribute endpoint
ne_cells dump out.nec | ne_cells compare a.nec b.nec
```

`algo.py`, `canon.py`, `ns_groups.py`, `pack.py` are byte-identical to the operator's copy
(the digest contract depends on it); `__main__.py` is the CLI. Determinism: canonical input
(7-decimal grid, `make_valid`), complete-copy-wins dedupe, WKB-sorted unions, round-half-up
quantisation, pinned GEOS. `tests/test_ne_cells.py` proves bit-identity under shuffled and
duplicated input and under the bevdirect two-cell split, on synthetic data.

## Tests

`python -m pytest tests` — no network: fake bevdirect-serve and fake public API, synthetic
village. Nothing fetched from the tiles is stored in this repository.

## Licence

Code MIT (`LICENSE`). Data reached through bevdirect-serve: BEV VTC, CC BY 4.0 — `© BEV,
<year> … bearbeitet` (`profile.notice()`, the document's `notice`, `X-Data-Attribution`) must be shown
with it. NE cells carry their own attribution in every container header.
