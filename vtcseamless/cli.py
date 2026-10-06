"""``vtcseamless`` command line.

  vtcseamless serve [--prefix DIR] [--port 8787]          download (once) + run bevdirect-serve in the foreground
  vtcseamless health                                      bevdirect-serve /health and the public API pin
  vtcseamless viewport W,S,E,N [--layers …] [-o f.json]   one /viewport document (blocks until ready)
  vtcseamless cell IX IY [-o f.json]                       one aligned grid cell document (24 h cache)
  vtcseamless observe --kg K [--kg K2 …] --observer LABEL [--epoch YYYY-MM] [--no-post] [-o DIR]
  vtcseamless report FILE.nec --observer LABEL [--no-post]
  vtcseamless manifest [--kg K] [--since CURSOR]           NE manifest (public)
  vtcseamless ne-cell H3 | ne-point LON LAT [--k N]        NE cells (public)
  vtcseamless kgs [--q NAME]                               KG register (public)

Environment: BEVDIRECT_URL (default http://127.0.0.1:8787), VTC_PUBLIC_API, VTC_TOKEN (contribute).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, profile
from .bevdirect import BevDirect, Server, ensure, install
from .observe import observe_many
from .public import PublicAPI


def _bbox(s: str):
    v = tuple(float(x) for x in s.split(","))
    if len(v) != 4:
        raise argparse.ArgumentTypeError("need W,S,E,N")
    return v


def _out(obj, path=None):
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if path:
        with open(path, "w") as f:
            f.write(s)
    else:
        print(s)


def _log(s):
    print(s, file=sys.stderr, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vtcseamless", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"vtcseamless-py {__version__} (bevdirect pin {profile.BEVDIRECT_TAG})")
    ap.add_argument("--bevdirect", default=profile.BEVDIRECT_URL, help="bevdirect-serve URL")
    ap.add_argument("--api", default=profile.PUBLIC_API, help="public API base URL")
    ap.add_argument("--token", default=profile.TOKEN, help="contributor token (VTC_TOKEN)")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/vtcseamless"), help="cell document cache dir ('' = none)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("serve"); p.add_argument("--prefix", default=os.path.expanduser("~/.local/share/vtcseamless"))
    p.add_argument("--port", type=int, default=8787); p.add_argument("--cells", type=int, default=160)
    p.add_argument("--install-only", action="store_true"); p.add_argument("--tag", default=profile.BEVDIRECT_TAG)
    sp.add_parser("health")
    p = sp.add_parser("viewport"); p.add_argument("bbox", type=_bbox); p.add_argument("--layers", default="parcels,footprints,landuse")
    p.add_argument("-o")
    p = sp.add_parser("cell"); p.add_argument("ix", type=int); p.add_argument("iy", type=int); p.add_argument("-o")
    p = sp.add_parser("observe"); p.add_argument("--kg", action="append", required=True); p.add_argument("--observer", required=True)
    p.add_argument("--epoch"); p.add_argument("--no-post", action="store_true"); p.add_argument("-o", "--out", default="./ne_out")
    p.add_argument("--bbox", type=_bbox, help="override the starting bbox guess"); p.add_argument("--keep-inputs", action="store_true")
    p.add_argument("--auto-serve", action="store_true", help="start bevdirect-serve locally if nothing answers at --bevdirect")
    p = sp.add_parser("report"); p.add_argument("file"); p.add_argument("--observer", required=True); p.add_argument("--no-post", action="store_true")
    p.add_argument("--bbox", type=_bbox)
    p = sp.add_parser("manifest"); p.add_argument("--kg"); p.add_argument("--since"); p.add_argument("--digests", action="store_true")
    p = sp.add_parser("ne-cell"); p.add_argument("h3")
    p = sp.add_parser("ne-point"); p.add_argument("lon", type=float); p.add_argument("lat", type=float); p.add_argument("--k", type=int, default=0)
    p = sp.add_parser("kgs"); p.add_argument("--q")
    a = ap.parse_args(argv)

    api = PublicAPI(a.api, a.token, log=_log)
    cache = a.cache or None

    if a.cmd == "serve":
        if a.install_only:
            print(install(a.prefix, a.tag or None, log=_log))
            return 0
        srv = Server(a.prefix, port=a.port, cells=a.cells, log=_log).start()
        try:
            _log(f"serving on {srv.url} — Ctrl-C to stop")
            srv.proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()
        return 0

    if a.cmd == "health":
        out = {"public_api": a.api}
        try:
            out["bevdirect"] = BevDirect(a.bevdirect).health()
        except Exception as e:  # noqa: BLE001
            out["bevdirect"] = {"ok": False, "error": str(e), "url": a.bevdirect}
        try:
            ne = PublicAPI(a.api, a.token, retry_s=0, timeout_s=15).ne()
            out["ne"] = {k: ne.get(k) for k in ("frozen", "kgs_built", "chunks")}
        except Exception as e:  # noqa: BLE001
            out["ne"] = {"error": str(e)}
        _out(out)
        return 0

    if a.cmd == "viewport":
        bev = BevDirect(a.bevdirect, cache, log=_log)
        _out(bev.viewport(*a.bbox, layers=a.layers.split(",")), a.o)
        return 0
    if a.cmd == "cell":
        bev = BevDirect(a.bevdirect, cache, log=_log)
        _out(bev.cell(a.ix, a.iy), a.o)
        return 0

    if a.cmd == "observe":
        srv = None
        if a.auto_serve:
            bev, srv = ensure(a.bevdirect, log=_log)
            bev = BevDirect(bev.url, cache, log=_log)
        else:
            bev = BevDirect(a.bevdirect, cache, log=_log)
        try:
            res = observe_many(a.kg, bev=bev, api=api, observer=a.observer, out_dir=a.out, epoch=a.epoch,
                               post=not a.no_post, bbox=a.bbox, keep_inputs=a.keep_inputs, log=_log)
        finally:
            if srv:
                srv.stop()
        _out(res)
        return 1 if any("error" in r for r in res) else 0

    if a.cmd == "report":
        from ne_cells.__main__ import report_of
        rep = report_of(a.file, a.observer, a.bbox)
        if a.no_post:
            _out(rep)
            return 0
        r = api.report(rep)
        _log(r.summary())
        _out(r.body)
        return 0

    if a.cmd == "manifest":
        m = api.ne_manifest(since=a.since, fields="digests" if a.digests else None)
        if a.kg:
            _out({a.kg.zfill(5): (m.get("kgs") or {}).get(a.kg.zfill(5)), "generated_at": m.get("generated_at"), "frozen": m.get("frozen")})
        else:
            _out(m)
        return 0
    if a.cmd == "ne-cell":
        _out(api.ne_cell(a.h3).data)
        return 0
    if a.cmd == "ne-point":
        _out(api.ne_cells(lon=a.lon, lat=a.lat, k=a.k).data)
        return 0
    if a.cmd == "kgs":
        rows = api.kgs()
        if a.q:
            q = a.q.lower()
            rows = [r for r in rows if q in (r.get("kg_name", "") + " " + r.get("gemeinde_name", "")).lower() or r.get("kg_code") == a.q.zfill(5)]
        _out(rows)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
