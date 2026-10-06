"""Client for ``bevdirect-serve`` (the Go assembler of the vtcseamless repo).

The server composes every answer from a fixed world grid of 0.02° cells, each assembled once
from the tiles and cached 6 h; ``/viewport?wait=0`` answers at once with ``ready:false,
pending:true, retry_after_s`` for cells still assembling. This client

* speaks that protocol (:meth:`BevDirect.viewport` loops on ``pending`` until ``ready`` or the
  deadline; a ``pending`` answer is UNKNOWN, never "no objects");
* exposes the grid (:func:`cell_of`, :func:`cells_for`, :func:`cell_bbox`) so that peers fetch
  the SAME aligned cells the operator baselines are built from;
* keeps a 24 h on-disk cache of ready cell documents (:attr:`BevDirect.cache_dir`) — the tile TTL
  of the server, so a client never holds a document older than the tiles behind it;
* can download the release binary and run it as a child process (:func:`install`,
  :class:`Server`) when no server is reachable.

Nothing here is keyed by object id: a single object is only ever obtained by asking for a
location (``/parcel/{id}?lon&lat``), a folio only for the part inside a bbox — exactly the
server's contract.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from . import profile

ALL_LAYERS = ("parcels", "footprints", "landuse")


class PendingError(RuntimeError):
    """The server did not become ready within the deadline. The partial document is attached
    as ``.document`` — it is UNKNOWN for the cells still assembling, not empty."""

    def __init__(self, msg: str, document: Optional[dict] = None):
        super().__init__(msg)
        self.document = document


class ServerError(RuntimeError):
    pass


# ── grid ────────────────────────────────────────────────────────────────────────────────

def cell_of(lon: float, lat: float) -> tuple[int, int]:
    """Grid cell (ix, iy) containing lon/lat: ``ix = floor(lon / 0.02)``."""
    return math.floor(lon / profile.CELL_SIZE), math.floor(lat / profile.CELL_SIZE)


def cell_bbox(ix: int, iy: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of grid cell (ix, iy) — the UNPADDED cell. Asking
    ``/viewport`` for exactly this box makes the server compose one cached cell."""
    w, s = ix * profile.CELL_SIZE, iy * profile.CELL_SIZE
    return w, s, w + profile.CELL_SIZE, s + profile.CELL_SIZE


def cells_for(west: float, south: float, east: float, north: float) -> list[tuple[int, int]]:
    """All grid cells intersecting the bbox (same rule as the server: a bbox ending exactly
    on a grid line does not include the next cell)."""
    lo = cell_of(west, south)
    hi = cell_of(east - 1e-9, north - 1e-9)
    return [(ix, iy) for ix in range(lo[0], hi[0] + 1) for iy in range(lo[1], hi[1] + 1)]


# ── client ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Stats:
    requests: int = 0
    cache_hits: int = 0
    pending_rounds: int = 0
    seconds: float = 0.0
    by_cell: dict = field(default_factory=dict)


class BevDirect:
    """HTTP client for one bevdirect-serve instance.

    ``cache_dir`` (optional) persists ready cell documents as ``cells/{ix}_{iy}.json.gz`` for
    ``doc_ttl_s`` (default: the server's 24 h tile TTL) and writes ``ATTRIBUTION.txt`` beside
    them. ``deadline_s`` bounds how long :meth:`viewport` waits for cold cells in total.
    """

    def __init__(self, url: str = profile.BEVDIRECT_URL, cache_dir: Optional[str] = None,
                 doc_ttl_s: int = profile.TILE_TTL_S, deadline_s: float = 300.0, timeout_s: float = 90.0,
                 log: Optional[Callable[[str], None]] = None):
        self.url = url.rstrip("/")
        self.cache_dir = cache_dir
        self.doc_ttl_s = doc_ttl_s
        self.deadline_s = deadline_s
        self.timeout_s = timeout_s
        self.log = log or (lambda s: None)
        self.stats = Stats()
        self._version: Optional[str] = None
        if cache_dir:
            os.makedirs(os.path.join(cache_dir, "cells"), exist_ok=True)
            _write_attribution(cache_dir, self.url)

    # ── raw HTTP ──────────────────────────────────────────────────────────
    def _get(self, path: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> tuple[int, dict, dict]:
        q = "?" + urllib.parse.urlencode(params) if params else ""
        req = urllib.request.Request(self.url + path + q, headers={
            "Accept": "application/json", "Accept-Encoding": "gzip",
            "User-Agent": profile.USER_AGENT.format(version=_pkg_version())})
        self.stats.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as r:
                raw, hdrs, status = r.read(), dict(r.headers), r.status
        except urllib.error.HTTPError as e:
            raw, hdrs, status = e.read(), dict(e.headers), e.code
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            raise ServerError(f"bevdirect-serve unreachable at {self.url}: {e}") from e
        if hdrs.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"error": raw[:200].decode("utf-8", "replace")}
        return status, body, hdrs

    # ── service endpoints ─────────────────────────────────────────────────
    def health(self) -> dict:
        status, body, _ = self._get("/health", timeout=10)
        if status != 200 or not body.get("ok"):
            raise ServerError(f"/health {status}: {body}")
        self._version = body.get("bevdirect_version") or self._version
        return body

    @property
    def version(self) -> str:
        """``bevdirect_version`` of the server (the NE source is ``bevdirect@<version>``)."""
        if self._version is None:
            self.health()
        return self._version or "dev"

    def viewport(self, west: float, south: float, east: float, north: float,
                 layers: Iterable[str] = ALL_LAYERS, deadline_s: Optional[float] = None) -> dict:
        """``GET /viewport`` for a bbox (edges ≤ 0.045°). Blocks until ``ready:true`` or the
        deadline, polling with ``wait=`` and honouring ``retry_after_s``. Raises
        :class:`PendingError` (document attached) when still pending at the deadline."""
        if east <= west or north <= south or east - west > profile.MAX_SPAN + 1e-9 or north - south > profile.MAX_SPAN + 1e-9:
            raise ValueError(f"bbox empty or edge > {profile.MAX_SPAN}°: {west},{south},{east},{north}")
        layers = ",".join(layers)
        t0 = time.time()
        end = t0 + (deadline_s if deadline_s is not None else self.deadline_s)
        wait = 20.0
        body: dict = {}
        while True:
            left = end - time.time()
            params = dict(west=repr(west), south=repr(south), east=repr(east), north=repr(north), layers=layers,
                          wait=f"{max(0.0, min(wait, left)):.1f}")
            status, body, _ = self._get("/viewport", params, timeout=max(15.0, min(wait, left) + 30))
            if status == 400:
                raise ValueError(body.get("error", "bad request"))
            if status != 200:
                # 502 = the server could not assemble (every tile failed, …) — transient; retry
                self.log(f"viewport {status}: {body.get('error')}")
                body = dict(body, ready=False, pending=True)
            if body.get("ready"):
                self.stats.seconds += time.time() - t0
                self._version = body.get("bevdirect_version") or self._version
                return body
            self.stats.pending_rounds += 1
            if time.time() >= end:
                self.stats.seconds += time.time() - t0
                raise PendingError(f"viewport {west},{south},{east},{north} still pending after {time.time()-t0:.0f}s", body)
            time.sleep(min(float(body.get("retry_after_s") or 3), max(0.0, end - time.time())))

    def cell(self, ix: int, iy: int, layers: Iterable[str] = ALL_LAYERS, deadline_s: Optional[float] = None,
             refresh: bool = False) -> dict:
        """The viewport document of one aligned grid cell — the unit the operator baselines
        and every peer build are made of. Served from the 24 h document cache when present
        (only ``ready:true`` documents are ever cached)."""
        key = f"{ix}_{iy}"
        layers = tuple(layers)
        if not refresh and layers == ALL_LAYERS:
            doc = self._load(key)
            if doc is not None:
                self.stats.cache_hits += 1
                return doc
        t0 = time.time()
        doc = self.viewport(*cell_bbox(ix, iy), layers=layers, deadline_s=deadline_s)
        self.stats.by_cell[key] = round(time.time() - t0, 2)
        if layers == ALL_LAYERS:
            self._store(key, doc)
        return doc

    def cells(self, cells: Iterable[tuple[int, int]], deadline_s: Optional[float] = None) -> dict[tuple[int, int], dict]:
        """Fetch several cells (sequentially — the server parallelises tiles and prefetches
        the neighbour ring itself; a client-side fan-out would only compete for its
        connections). Returns ``{(ix, iy): document}``; raises on the first cell that stays pending."""
        out = {}
        for ix, iy in cells:
            doc = self.cell(ix, iy, deadline_s=deadline_s)
            took = self.stats.by_cell.get(f"{ix}_{iy}")
            self.log(f"cell {ix}/{iy}: {len(doc.get('parcels') or [])} parcels, {len(doc.get('footprints') or [])} fp, "
                     f"{len(doc.get('landuse') or [])} lu ({'cache' if took is None else f'{took} s'})")
            out[(ix, iy)] = doc
        return out

    def parcel(self, parcel_id: str, lon: float, lat: float) -> Optional[dict]:
        """``GET /parcel/{id}?lon&lat`` — resolved from the tiles around the location. None if absent."""
        status, body, _ = self._get("/parcel/" + urllib.parse.quote(parcel_id, safe=""), dict(lon=lon, lat=lat))
        if status == 404:
            return None
        if status != 200:
            raise ServerError(f"/parcel {status}: {body}")
        return body.get("parcel")

    def ez(self, kg: str, ez: str, lon: Optional[float] = None, lat: Optional[float] = None,
           bbox: Optional[tuple[float, float, float, float]] = None) -> dict:
        """``GET /ez`` — the folio's objects INSIDE the given area only (``partial:true`` always)."""
        p: dict[str, Any] = dict(kg=kg, ez=ez)
        if bbox:
            p.update(west=bbox[0], south=bbox[1], east=bbox[2], north=bbox[3])
        else:
            p.update(lon=lon, lat=lat)
        status, body, _ = self._get("/ez", p)
        if status != 200:
            raise ServerError(f"/ez {status}: {body}")
        return body

    def municipality(self, lon: float, lat: float) -> Optional[dict]:
        status, body, _ = self._get("/municipality", dict(lon=lon, lat=lat))
        return body if status == 200 else None

    def municipalities(self, q: str, limit: int = 20) -> list[dict]:
        status, body, _ = self._get("/municipalities", dict(q=q, limit=limit))
        return (body.get("results") or []) if status == 200 else []

    def kg(self, code: str) -> Optional[dict]:
        """``GET /kg/{kg}`` — admin row (names, bbox, area) from the server's embedded VGD table."""
        status, body, _ = self._get("/kg/" + pad5(code))
        return body.get("kg") if status == 200 else None

    # ── document cache ────────────────────────────────────────────────────
    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, "cells", key + ".json.gz")  # type: ignore[arg-type]

    def _load(self, key: str) -> Optional[dict]:
        if not self.cache_dir:
            return None
        p = self._path(key)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return None
        if time.time() - st.st_mtime > self.doc_ttl_s:
            try:
                os.remove(p)
            except OSError:
                pass
            return None
        with gzip.open(p, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if doc.get("ready") else None

    def _store(self, key: str, doc: dict) -> None:
        if not self.cache_dir or not doc.get("ready"):
            return
        p = self._path(key)
        tmp = p + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)

    def sweep(self) -> int:
        """Delete cached documents older than ``doc_ttl_s``; returns the count removed."""
        if not self.cache_dir:
            return 0
        n, cut = 0, time.time() - self.doc_ttl_s
        d = os.path.join(self.cache_dir, "cells")
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.stat(p).st_mtime < cut:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
        return n


def pad5(code: str) -> str:
    code = str(code).strip()
    return code.zfill(5) if code.isdigit() and len(code) < 5 else code


def _pkg_version() -> str:
    from . import __version__
    return __version__


def _write_attribution(cache_dir: str, server: str) -> None:
    with open(os.path.join(cache_dir, "ATTRIBUTION.txt"), "w", encoding="utf-8") as f:
        f.write(f"{profile.notice()}\n"
                f"Licence: CC BY 4.0 — {profile.LICENSE_URL}\n"
                "Documents in cells/ are bevdirect-serve /viewport answers (objects re-assembled from the BEV vector\n"
                "tiles, re-projected, 7-decimal WGS84). Not an official extract; not operated by or affiliated with\n"
                f"BEV. Each document carries fetched_at — show that date wherever the data is displayed. Source: {server}\n"
                f"Documents expire after {profile.TILE_TTL_S // 3600} h (the server's tile TTL).\n")


# ── binary install / child process ──────────────────────────────────────────────────────

def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise ServerError(f"no bevdirect-serve release for {m}; build from source: {profile.GO_REPO}")


def install(prefix: str, tag: Optional[str] = profile.BEVDIRECT_TAG, log: Callable[[str], None] = print) -> str:
    """Download the bevdirect-serve release tarball (static linux binary) for this machine into
    ``prefix/bevdirect-serve`` and return its path. ``tag=None`` = latest release. No root, no
    Go toolchain, no credentials. For a system service use the repo's ``bootstrap.sh`` instead."""
    if platform.system() != "Linux":
        raise ServerError(f"release binaries are linux only; build from source: {profile.GO_REPO}")
    arch = _arch()
    api = profile.GO_RELEASES_API + (f"/tags/{tag}" if tag else "/latest")
    req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "vtcseamless-py"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    want = f"linux_{arch}.tar.gz"
    url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"].endswith(want)), None)
    if not url:
        raise ServerError(f"release {rel.get('tag_name')} has no {want} asset")
    os.makedirs(prefix, exist_ok=True)
    log(f"downloading bevdirect-serve {rel.get('tag_name')} ({arch}) → {prefix}")
    with tempfile.TemporaryDirectory() as td:
        tgz = os.path.join(td, "rel.tar.gz")
        urllib.request.urlretrieve(url, tgz)
        with tarfile.open(tgz) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/bevdirect-serve") or m.name == "bevdirect-serve")
            tf.extract(member, td, filter="data")
            src = os.path.join(td, member.name)
        dst = os.path.join(prefix, "bevdirect-serve")
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o755)
    with open(os.path.join(prefix, "VERSION"), "w") as f:
        f.write(rel.get("tag_name", "") + "\n")
    return dst


class Server:
    """Run bevdirect-serve as a child process (flags = the service unit's defaults: tile TTL
    24 h, cell TTL 6 h, 160 cells, 24 connections, 1 GiB in-memory tile cache). Use as a
    context manager or call :meth:`start` / :meth:`stop`.

    bevdirect-serve keeps BEV tiles **in RAM only** (never on disk); ``cache_dir`` is
    accepted for backwards compatibility and ignored. Start one Server for a whole batch
    of KGs so the tiles of neighbouring KGs stay hot (``tile_cache_mb`` ≈ 1024 holds ~200 KGs)."""

    def __init__(self, prefix: str, port: int = 8787, cache_dir: Optional[str] = None, cells: int = 160,
                 max_conns: int = 24, tile_ttl: str = "24h", cell_ttl: str = "6h", tile_cache_mb: int = 1024,
                 extra_args: Iterable[str] = (), log: Callable[[str], None] = print):
        self.prefix, self.port, self.log = prefix, port, log
        self.binary = os.path.join(prefix, "bevdirect-serve")
        self.cache_dir = cache_dir  # deprecated: tiles are never written to disk
        self.tile_cache_mb = tile_cache_mb
        self.args = [self.binary, "-addr", f"127.0.0.1:{port}", "-ttl", cell_ttl, "-tile-ttl", tile_ttl,
                     "-cells", str(cells), "-max-conns", str(max_conns), *extra_args]
        self.proc: Optional[subprocess.Popen] = None
        self.url = f"http://127.0.0.1:{port}"

    def start(self, install_if_missing: bool = True, ready_timeout_s: float = 30.0) -> "Server":
        if not os.path.exists(self.binary):
            if not install_if_missing:
                raise ServerError(f"{self.binary} missing")
            install(self.prefix, log=self.log)
        args = list(self.args)
        if self._supports_tile_cache_flag():
            args[1:1] = ["-tile-cache-mb", str(self.tile_cache_mb)]
        logf = open(os.path.join(self.prefix, "bevdirect-serve.log"), "ab")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=logf)
        client = BevDirect(self.url)
        end = time.time() + ready_timeout_s
        while time.time() < end:
            try:
                h = client.health()
                self.log(f"bevdirect-serve {h.get('bevdirect_version')} on {self.url} ({h.get('admin_source')})")
                return self
            except ServerError:
                if self.proc.poll() is not None:
                    raise ServerError(f"bevdirect-serve exited with {self.proc.returncode}; see {self.prefix}/bevdirect-serve.log")
                time.sleep(0.3)
        raise ServerError("bevdirect-serve did not become ready")

    def _supports_tile_cache_flag(self) -> bool:
        """v0.3.x binaries reject unknown flags; only pass -tile-cache-mb when -h lists it."""
        try:
            h = subprocess.run([self.binary, "-h"], capture_output=True, text=True, timeout=10)
            return "-tile-cache-mb" in (h.stdout + h.stderr)
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def ensure(url: str = profile.BEVDIRECT_URL, prefix: Optional[str] = None,
           log: Callable[[str], None] = print) -> tuple[BevDirect, Optional[Server]]:
    """Return a healthy client for ``url``; when nothing answers there and the URL is local,
    install the release binary under ``prefix`` (default ``~/.local/share/vtcseamless``) and
    start it as a child process on that port. The caller stops the returned :class:`Server`."""
    client = BevDirect(url)
    try:
        client.health()
        return client, None
    except ServerError:
        pass
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ServerError(f"no bevdirect-serve at {url} and it is not local — start one there or point BEVDIRECT_URL elsewhere")
    port = urllib.parse.urlparse(url).port or 8787
    srv = Server(prefix or os.path.expanduser("~/.local/share/vtcseamless"), port=port, log=log).start()
    return BevDirect(srv.url), srv
