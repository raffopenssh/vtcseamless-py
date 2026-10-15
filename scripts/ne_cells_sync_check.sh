#!/usr/bin/env bash
# Mirror of the operator's scripts/ne_cells_sync_check.sh: the frozen ne_cells files in this
# package MUST be byte-identical to the operator's copy (the digest contract depends on it).
# Compares against a local clone of the operator repo (CLONE=…, default ~/cadastre-process-api).
# Tile hashing / QC provenance lives in vtcseamless/observe.py, never in ne_cells/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
THEIRS=${CLONE:-$HOME/cadastre-process-api}/ne_cells
FILES="algo.py canon.py ns_groups.py pack.py __init__.py"
rc=0
for f in $FILES; do
  if cmp -s "$HERE/ne_cells/$f" "$THEIRS/$f"; then echo "ok    ne_cells/$f"; else echo "DIFF  ne_cells/$f"; rc=1; fi
done
# __main__.py differs by design (the operator keeps an operator-only subcommand);
# change.py is not frozen but mirrored by copy — warn, do not fail.
if ! cmp -s "$HERE/ne_cells/change.py" "$THEIRS/change.py"; then echo "WARN  ne_cells/change.py differs (mirror by copy)"; fi
if grep -q "chunks_tiles\|tile_provenance\|tiles_expected" "$HERE"/ne_cells/*.py 2>/dev/null; then echo "DIFF  tile hashing must not live in ne_cells/"; rc=1; fi
exit $rc
