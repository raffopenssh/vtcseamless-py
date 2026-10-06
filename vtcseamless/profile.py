"""Service profile — the ONLY place in this package that names hosts, source strings and the
attribution text. Everything else imports from here; the word-ban test (tests/test_wordban.py)
enforces that.

Override at runtime with environment variables (BEVDIRECT_URL, VTC_PUBLIC_API, VTC_TOKEN).
"""
import os
import time

# ── bevdirect-serve (the Go assembler; this package never fetches tiles itself) ───────────
GO_REPO = "https://github.com/raffopenssh/vtcseamless"
GO_RELEASES_API = "https://api.github.com/repos/raffopenssh/vtcseamless/releases"
GO_BOOTSTRAP = "https://raw.githubusercontent.com/raffopenssh/vtcseamless/main/bootstrap.sh"
#: pinned tag of bevdirect-serve this package was validated against; the NE source string is
#: "bevdirect@<tag>" and documents from another tag are a different source (never compared).
BEVDIRECT_TAG = "v0.3.0"
BEVDIRECT_URL = os.environ.get("BEVDIRECT_URL", "http://127.0.0.1:8787")

# ── the public overlay API (tokenless reads; /contrib needs a contributor token) ─────────
PUBLIC_API = os.environ.get("VTC_PUBLIC_API", "https://umfeld-at.exe.xyz").rstrip("/")
#: contributor (or peer) token for POST /contrib/api/v1/ne/{kg}/report — never a default here.
TOKEN = os.environ.get("VTC_TOKEN", "")
CONTRIB_PREFIX = "/contrib"

# ── licence ─────────────────────────────────────────────────────────────────────────────
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


def notice(year: int | None = None) -> str:
    """Attribution string that must be rendered with any output derived from the tiles
    (BEV Nutzungsbedingungen §2.3.3: „© BEV, JJJJ“ on every copy, also after processing)."""
    y = year or time.gmtime().tm_year
    return f"© BEV, {y} – Datenquelle: Bundesamt für Eich- und Vermessungswesen, Kataster (CC BY 4.0), bearbeitet"


# ── client defaults (same numbers as the Go service) ────────────────────────────────────
CELL_SIZE = 0.02        # ° — bevdirect-serve's fixed world grid
CELL_PAD = 0.004        # ° — assembly pad; an object crossing a cell edge is complete in ≥ 1 cell
MAX_SPAN = 0.045        # ° per request edge accepted by /viewport
TILE_TTL_S = 24 * 3600  # bevdirect-serve tile cache TTL (-tile-ttl 24h); documents older than this are re-fetched
CELL_TTL_S = 6 * 3600   # bevdirect-serve assembled-cell TTL (-ttl 6h)
USER_AGENT = "vtcseamless-py/{version} (+https://github.com/raffopenssh/vtcseamless-py)"
