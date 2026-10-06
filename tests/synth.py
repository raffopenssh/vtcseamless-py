"""Synthetic bevdirect-style documents for the tests — NO real data in this repository.

A small village on a regular grid straddling the 0.02° cell edge at lon = 15.12: square
"parcels" of two KGs, a footprint on every third, land-use pieces covering each parcel. Objects
crossing the cell edge appear in both cells' documents, `complete:false` in the one where
they are cut — exactly what bevdirect-serve emits."""
import random

CELL = 0.02
ORIGIN_LON, ORIGIN_LAT = 15.11525, 47.1256  # SW corner; lon 15.11525–15.12525 → squares straddle 15.12; lat + 0.004° pad stays in one cell row
STEP = 0.0005
N = 20                                      # 20 × 20 squares
KG_WEST, KG_EAST = "99901", "99902"         # fictitious codes
VERSION = "v0.3.0"


def _sq(x, y, d):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + d, y], [x + d, y + d], [x, y + d], [x, y]]]}


def _r7(v):
    return round(v, 7)


def objects(seed=1, big=True):
    """(parcels, footprints, landuse) as plain rows in the /viewport schema, whole village.
    ``big`` adds two objects wider than the assembly pad (truncated copies)."""
    rnd = random.Random(seed)
    P, F, L = [], [], []
    n = 0
    for i in range(N):
        for j in range(N):
            x, y = _r7(ORIGIN_LON + i * STEP), _r7(ORIGIN_LAT + j * STEP)
            kg = KG_WEST if i < N // 2 else KG_EAST
            n += 1
            gnr = f"{n}" if rnd.random() < 0.8 else f"{n}/1"
            P.append({"parcel_id": f"{kg}-{gnr}", "kg_code": kg, "gnr": gnr, "ez": str(1 + n // 3), "rstatus": "G" if rnd.random() < 0.6 else "E",
                      "area_sqm": _r7(STEP * 111320 * STEP * 75800), "lon": _r7(x + STEP / 2), "lat": _r7(y + STEP / 2),
                      "complete": True, "parts": 1, "geometry": _sq(x, y, STEP)})
            ns = rnd.choice([48, 48, 52, 56, 95, 63])
            L.append({"id": f"lu{n}", "ns": ns, "tile": "16/0/0", "area_sqm": 1.0, "geometry": _sq(x, y, STEP)})
            if n % 3 == 0:
                d = STEP * 0.4
                F.append({"id": f"fp{n}", "ns": 41, "tile": "16/0/0", "area_sqm": 1.0, "geometry": _sq(_r7(x + STEP * 0.3), _r7(y + STEP * 0.3), _r7(d))})
    # two wide objects south of the grid (same cell row), crossing the cell edge AND the 0.004° pad: A is
    # truncated in the west cell's document and complete in the east one; B is truncated in both (union rule).
    y = _r7(ORIGIN_LAT - 3 * STEP)
    for kg, gnr, x0, x1 in (() if not big else ((KG_EAST, "9001", 15.1170, 15.1245), (KG_EAST, "9002", 15.1140, 15.1260))):
        n += 1
        P.append({"parcel_id": f"{kg}-{gnr}", "kg_code": kg, "gnr": gnr, "ez": "900", "rstatus": "G", "area_sqm": 1000.0,
                  "lon": _r7((x0 + x1) / 2), "lat": _r7(y + STEP), "complete": True, "parts": 1,
                  "geometry": {"type": "Polygon", "coordinates": [[[x0, y], [x1, y], [x1, _r7(y + 2 * STEP)], [x0, _r7(y + 2 * STEP)], [x0, y]]]}})
        L.append({"id": f"lu{n}", "ns": 48, "tile": "16/0/0", "area_sqm": 1.0, "geometry": P[-1]["geometry"]})
    return P, F, L


def _bounds(g):
    xs = [p[0] for p in g["coordinates"][0]]
    ys = [p[1] for p in g["coordinates"][0]]
    return min(xs), min(ys), max(xs), max(ys)


def _clip(g, w, s, e, n):
    x0, y0, x1, y1 = _bounds(g)
    cx0, cy0, cx1, cy1 = max(x0, w), max(y0, s), min(x1, e), min(y1, n)
    if cx1 <= cx0 or cy1 <= cy0:
        return None, False
    whole = (cx0, cy0, cx1, cy1) == (x0, y0, x1, y1)
    return {"type": "Polygon", "coordinates": [[[cx0, cy0], [cx1, cy0], [cx1, cy1], [cx0, cy1], [cx0, cy0]]]}, whole


def cell_document(ix, iy, seed=1, pad=0.004, version=VERSION, big=True):
    """What /viewport answers for the aligned cell (ix, iy): objects whose bbox intersects the cell,
    clipped to the padded assembly box (so copies crossing the pad are complete:false)."""
    w, s = ix * CELL, iy * CELL
    e, n = w + CELL, s + CELL
    P, F, L = objects(seed, big)

    def keep(rows, clip_pad):
        out = []
        for r in rows:
            x0, y0, x1, y1 = _bounds(r["geometry"])
            if x1 < w or x0 > e or y1 < s or y0 > n:
                continue
            g, whole = _clip(r["geometry"], w - clip_pad, s - clip_pad, e + clip_pad, n + clip_pad)
            if g is None:
                continue
            r = dict(r, geometry=g)
            if "complete" in r:
                r["complete"] = whole
            out.append(r)
        return out

    return {"parcels": keep(P, pad), "footprints": keep(F, pad), "landuse": keep(L, pad), "ready": True, "truncated": False,
            "count": 0, "source": "bev-direct", "bevdirect_version": version, "coord_decimals": 7,
            "notice": "© BEV, 2026 – test", "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "fetched_at": "2026-10-06T00:00:00Z", "stats": {}, "query_time_ms": 1}


def village_cells():
    """The two cells the synthetic village spans."""
    import math
    return sorted({(math.floor(ORIGIN_LON / CELL), math.floor(ORIGIN_LAT / CELL)),
                   (math.floor((ORIGIN_LON + N * STEP) / CELL), math.floor(ORIGIN_LAT / CELL))})
