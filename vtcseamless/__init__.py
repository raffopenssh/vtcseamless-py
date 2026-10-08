"""vtcseamless — Python client side of the vtcseamless / bevdirect stack.

The tile assembler stays the Go service ``bevdirect-serve`` (one binary, no credentials);
this package drives it and does everything a Python peer needs around it:

* :mod:`vtcseamless.bevdirect` — ``/viewport`` client on bevdirect-serve's 0.02° cell grid
  (``wait=0`` retry loop, ``complete``-aware dedupe, 24 h document cache), plus
  ``install()`` / ``Server`` to download the release binary and run it as a child process;
* :mod:`ne_cells` — the frozen reference implementation of the NE cell statistics
  (algo ``ne-cells-2``; pinned shapely 2.1.2 + h3 4.5.0);
* :mod:`vtcseamless.public` — the public overlay API (NE manifest/chunks/cells, KG register,
  ``/context``) and the contributor report endpoint;
* :mod:`vtcseamless.observe` — the peer loop: KG → cells → build → report;
* :mod:`vtcseamless.cache` — licence-compliant response cache (attribution + date stored with
  every entry, ETag revalidation, pending answers never cached).

Hosts, source pins and the attribution text live only in :mod:`vtcseamless.profile`.
"""
__version__ = "0.1.1"

from . import profile  # noqa: F401
from .bevdirect import BevDirect, Server, install, cells_for, cell_bbox, cell_of  # noqa: F401
from .public import PublicAPI, ReportResult  # noqa: F401
from .observe import observe  # noqa: F401
