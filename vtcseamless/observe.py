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
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Iterable, Optional

from ne_cells import canon
from ne_cells.__main__ import build_from_bevdirect, report_of
from ne_cells.algo import INPUT_PAD_DEG, default_input_bbox

from . import profile
from .bevdirect import BevDirect, cells_for
from .public import APIError, PublicAPI, ReportResult


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
    rep = report_of(nec, observer)
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
                       since_last_changed=len(res.since_last.get("changed") or []))
        log(res.summary())
    return summary


def observe_many(kgs: Iterable[str], **kw) -> list[dict]:
    out = []
    for kg in kgs:
        try:
            out.append(observe(kg, **kw))
        except (APIError, ValueError, RuntimeError) as e:  # one bad KG must not stop a pass
            kw.get("log", print)(f"FAIL {kg}: {e}")
            out.append(dict(kg=str(kg).zfill(5), error=str(e)))
    return out
