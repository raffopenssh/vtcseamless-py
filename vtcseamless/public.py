"""Client for the public overlay API (tokenless reads) and the contributor report endpoint.

Public reads used by peers (all keyed by H3 cell, lon/lat, bbox or KG code — never by object):

* ``GET /api/v1/ne`` · ``/ne/manifest[?since=|?fields=digests]`` · ``/ne/{kg}`` (binary
  container) · ``/ne/{kg}/head`` · ``/ne/chunk/{h3_res9}`` · ``/ne/cell/{h3}`` ·
  ``/ne/cells?ids=|lon&lat[&k]|bbox[&list=1]`` — the declared NE layer;
* ``GET /api/v1/kgs`` — the KG register (codes, names, admin hierarchy, bbox; 7 850 rows);
* ``GET /api/v1/context?lon&lat[&include=…]`` — the one-call overlay bundle for a point.

Contribute (token): ``POST {CONTRIB_PREFIX}/api/v1/ne/{kg}/report`` with an epoch report
(digests only — :func:`ne_cells.pack.epoch_report`). 2 POST/s per token, burst 30; a 429
carries ``Retry-After`` and is honoured here, never retried early.

Everything 200 is returned with the ``X-Data-Attribution`` header kept (``Answer.notice``);
``202``/``pending`` answers are passed through, never cached (:mod:`vtcseamless.cache` does
the persistent caching when you want it).
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from . import profile


class APIError(RuntimeError):
    def __init__(self, status: int, body: Any, path: str):
        super().__init__(f"{path}: HTTP {status}: {str(body)[:300]}")
        self.status, self.body, self.path = status, body, path


@dataclass
class Answer:
    status: int
    data: Any                 # parsed JSON, or bytes for binary answers
    headers: dict
    notice: str               # X-Data-Attribution (render with the data)
    etag: str

    @property
    def pending(self) -> bool:
        d = self.data if isinstance(self.data, dict) else {}
        return self.status == 202 or d.get("pending") is True or d.get("ready") is False


@dataclass
class ReportResult:
    """Answer of ``POST …/ne/{kg}/report`` (see the contract doc § Observers)."""
    kg: str
    status: int
    body: dict

    @property
    def compared(self) -> bool:
        return bool(self.body.get("compared"))

    @property
    def baseline(self) -> str:
        return str(self.body.get("baseline", ""))

    @property
    def unchanged(self) -> bool:
        return bool(self.body.get("unchanged"))

    @property
    def chunks_changed(self) -> list:
        return list(self.body.get("chunks_changed") or [])

    @property
    def chunks_same(self) -> int:
        v = self.body.get("chunks_same")
        return len(v) if isinstance(v, list) else int(v or 0)

    @property
    def since_last(self) -> dict:
        return dict(self.body.get("since_last") or {})

    @property
    def want_chunks(self) -> list:
        """Chunks whose statistics rows the server asks for (change protocol step 2)."""
        return list(self.body.get("want_chunks") or [])

    def summary(self) -> str:
        b = self.body
        sl = self.since_last
        return (f"kg {self.kg}: baseline={self.baseline} compared={self.compared} unchanged={self.unchanged} "
                f"same={self.chunks_same} changed={len(self.chunks_changed)} "
                f"unknown_to_us={len(b.get('chunks_unknown_to_us') or [])} baselined_now={b.get('chunks_baselined_now')} "
                f"since_last.changed={len(sl.get('changed') or [])} token={b.get('token')}")


class PublicAPI:
    """One base URL (default :data:`profile.PUBLIC_API`), optional token for the contribute
    prefix. ``retry_s`` bounds how long a request rides out 429/5xx/transport errors."""

    def __init__(self, base: str = profile.PUBLIC_API, token: str = profile.TOKEN, timeout_s: float = 120.0,
                 retry_s: float = 600.0, log: Optional[Callable[[str], None]] = None):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self.retry_s = retry_s
        self.log = log or (lambda s: None)
        self._etags: dict[str, tuple[str, Answer]] = {}

    # ── transport ─────────────────────────────────────────────────────────
    def request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[bytes] = None,
                headers: Optional[dict] = None, auth: bool = False, binary: bool = False,
                use_etag: bool = True) -> Answer:
        url = self.base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        hdrs = {"Accept-Encoding": "gzip", "User-Agent": profile.USER_AGENT.format(version=_pkg_version()),
                "Accept": "application/octet-stream" if binary else "application/json"}
        if headers:
            hdrs.update(headers)
        if auth:
            if not self.token:
                raise APIError(0, "no token: set VTC_TOKEN or PublicAPI(token=…)", path)
            hdrs["Authorization"] = "Bearer " + self.token
        cached = self._etags.get(url) if (use_etag and method == "GET") else None
        if cached:
            hdrs["If-None-Match"] = cached[0]
        end = time.time() + self.retry_s
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    status, rh, raw = r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                status, rh, raw = e.code, e.headers, e.read()
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                if time.time() > end:
                    raise APIError(0, f"unreachable: {e}", path) from e
                wait = min(5 * attempt, 30)
                self.log(f"{path}: {e} — retry in {wait}s")
                time.sleep(wait)
                continue
            if status == 304 and cached:
                return cached[1]
            if status in (429, 502, 503, 504) and time.time() < end:
                ra = _retry_after(rh) or min(5 * attempt, 30)
                self.log(f"{path}: HTTP {status} — retry in {ra}s")
                time.sleep(ra)
                continue
            if rh.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            if binary and status == 200:
                data: Any = raw
            else:
                try:
                    data = json.loads(raw) if raw else None
                except ValueError:
                    data = raw
            notice = _latin1_fix(rh.get("X-Data-Attribution", ""))
            ans = Answer(status, data, dict(rh), notice, rh.get("ETag", ""))
            if status >= 400 and status != 404:
                raise APIError(status, data, path)
            if method == "GET" and status == 200 and ans.etag and use_etag:
                self._etags[url] = (ans.etag, ans)
            return ans

    def get(self, path: str, params: Optional[dict] = None, **kw) -> Answer:
        return self.request("GET", path, params, **kw)

    # ── NE declared layer ─────────────────────────────────────────────────
    def ne(self) -> dict:
        """``GET /api/v1/ne`` — frozen pin, endpoints, coverage counts, dictionary ids."""
        return self.get("/api/v1/ne").data

    def ne_manifest(self, since: Optional[str] = None, fields: Optional[str] = None) -> dict:
        """Full manifest, or the delta since ``generated_at`` of your last answer
        (``delta:true``; a too-old cursor comes back as a full body, ``delta:false`` — replace)."""
        p = {}
        if since:
            p["since"] = since
        if fields:
            p["fields"] = fields
        return self.get("/api/v1/ne/manifest", p or None).data

    def ne_kg(self, kg: str, layers: Optional[str] = None, as_json: bool = False) -> Answer:
        """Whole-KG container (``application/vnd.ne-cells`` bytes; ``as_json`` decodes server-side).
        404 → ``Answer.status == 404`` with ``{status, queue_position, retry_after_s}`` in ``data``."""
        p = {}
        if layers:
            p["layers"] = layers
        if as_json:
            p["format"] = "json"
        return self.get(f"/api/v1/ne/{kg}", p or None, binary=not as_json)

    def ne_head(self, kg: str) -> Answer:
        return self.get(f"/api/v1/ne/{kg}/head")

    def ne_chunk(self, h3_res9: str, fmt: str = "json") -> Answer:
        """The primitive: one res-9 chunk (``format=zstd`` bytes + ``X-NE-Dict``, ``bin`` rows, or json)."""
        return self.get(f"/api/v1/ne/chunk/{h3_res9}", {"format": fmt}, binary=fmt != "json")

    def ne_dict(self, dict_id: str) -> bytes:
        return self.get(f"/api/v1/ne/dict/{dict_id}", binary=True).data

    def ne_cell(self, h3: str) -> Answer:
        return self.get(f"/api/v1/ne/cell/{h3}")

    def ne_cells(self, ids: Optional[Iterable[str]] = None, lon: Optional[float] = None, lat: Optional[float] = None,
                 k: int = 0, bbox: Optional[tuple] = None, list_only: bool = False, fmt: str = "json") -> Answer:
        p: dict[str, Any] = {"format": fmt}
        if ids:
            p["ids"] = ",".join(ids)
        elif bbox:
            p["bbox"] = ",".join(repr(float(x)) for x in bbox)
            if list_only:
                p["list"] = 1
        else:
            p.update(lon=lon, lat=lat, k=k)
        return self.get("/api/v1/ne/cells", p, binary=fmt == "bin")

    # ── register / context ────────────────────────────────────────────────
    def kgs(self) -> list[dict]:
        """The KG register: ``[{kg_code, kg_name, gemeinde_code, …, bbox:[w,s,e,n], centroid}]``."""
        d = self.get("/api/v1/kgs").data
        return (d.get("data") or d).get("kgs") or []

    def kg(self, code: str) -> Optional[dict]:
        code = str(code).zfill(5)
        return next((r for r in self.kgs() if r.get("kg_code") == code), None)

    def context(self, lon: float, lat: float, include: Optional[str] = None, **extra) -> Answer:
        p: dict[str, Any] = dict(lon=lon, lat=lat, **extra)
        if include:
            p["include"] = include
        return self.get("/api/v1/context", p)

    # ── contribute ────────────────────────────────────────────────────────
    def report(self, report: dict) -> ReportResult:
        """``POST {CONTRIB_PREFIX}/api/v1/ne/{kg}/report`` — the body is exactly what
        ``ne_cells report`` prints (digests only). Honours 429/Retry-After."""
        kg = str(report["kg"]).zfill(5)
        raw = json.dumps(report, separators=(",", ":"), ensure_ascii=True).encode()
        ans = self.request("POST", f"{profile.CONTRIB_PREFIX}/api/v1/ne/{kg}/report", body=raw,
                           headers={"Content-Type": "application/json"}, auth=True, use_etag=False)
        if ans.status == 404:
            raise APIError(404, "contribute prefix answered 404: token missing or not a contributor token", f"/ne/{kg}/report")
        return ReportResult(kg, ans.status, ans.data if isinstance(ans.data, dict) else {})

    def chunks(self, kg: str, observer: str, body: bytes) -> Answer:
        """``POST {CONTRIB_PREFIX}/api/v1/ne/{kg}/chunks?observer=`` — the NECH body
        (``ne_cells.change.pack_chunk_rows``) of exactly the ``want_chunks`` the report answer asked
        for: per-cell statistics rows with the register bytes zeroed. Never K rows, never inputs."""
        kg = str(kg).zfill(5)
        return self.request("POST", f"{profile.CONTRIB_PREFIX}/api/v1/ne/{kg}/chunks", params={"observer": observer}, body=body,
                            headers={"Content-Type": "application/octet-stream"}, auth=True, use_etag=False)

    def reports(self, kg: str) -> Answer:
        """``GET …/ne/{kg}/report`` — reports received for a KG (token)."""
        return self.request("GET", f"{profile.CONTRIB_PREFIX}/api/v1/ne/{str(kg).zfill(5)}/report", auth=True, use_etag=False)

    def manifest_entry(self, kg: str) -> dict:
        """One KG of the manifest via the contribute prefix (works for contributor tokens too)."""
        m = self.ne_manifest()
        return (m.get("kgs") or {}).get(str(kg).zfill(5)) or {"status": "not_in_index"}


def _retry_after(h) -> int:
    try:
        return int(h.get("Retry-After") or 0)
    except ValueError:
        return 0


def _latin1_fix(s: str) -> str:
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _pkg_version() -> str:
    from . import __version__
    return __version__
