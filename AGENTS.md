# AGENTS.md — vtcseamless-py (read first; keep short)

Layout: `vtcseamless/` (client, observe loop, public API, cache) · `ne_cells/` (**frozen**
reference algorithm: `algo.py canon.py ns_groups.py pack.py` byte-identical to the operator's
copy — never edit without the operator; `change.py`, `__main__.py` are not frozen) ·
`tests/` (`python -m pytest tests`, no network).

## Downstream findings to fold into the next release (operator peer fleet, Oct 2026)

Reference code lives in the operator's peer pipeline (`ne_build_tiled.py`, `vct_pilot.py`);
port the behaviour, not the files.

1. **`ne_cells.algo.build_cells` memory is ~3.4 KB per product cell** (dense per-cell
   accumulator ≈ 1.1 KB + `rows` ≈ 2.3 KB). Measured: 168 k cells → +555 MB, HWM 772 MB; a
   1.5 M-cell KG needs 5–6 GB and is SIGKILLed (rc=-9, empty stderr) inside an 8 GB cgroup it
   shares with bevdirect-serve and the caller. **Wanted: a tiled build** (`build_cells_tiled`):
   above ~300 k estimated cells (`area / 307.1 m²`) cut `input_bbox` into an `nx×ny` grid of
   cores (~120 k cells each), build every core with the sub-domain padded by 0.006° (≥ a res-10
   cell + its res-12 children) and clipped to the global bbox, keep a cell / K row only from the
   sub-build whose core contains its H3 centre, pack `LU_REC`/`K_REC` per sub-build (`pack_records`
   is an id-sorted concatenation) and `gc` between tiles. Output is **byte-identical** to the
   monolithic build (verified on 168 k and 1.5 M cells, digest equal to four independent
   observers) because the algorithm is a function of the input domain (`INPUT_RULE`; cells not
   fully inside the domain are withheld). Header `stats`: object/domain counts exact;
   `slivers_omitted`, `no_own_share`, `kcells_withheld_coverage` summed over sub-builds
   (double-count the overlap bands — provenance only, not in the digest/report);
   `cells_outside_domain` omitted; add `stats.tiled = <sub-build count>`. Peak ≈ one sub-build
   + ~100 B per packed cell (168 k cells: 388 MB vs 763 MB).
2. **Reuse a kept `.nec` instead of rebuilding** when the caller only needs the assembled
   objects again (`observe`/CLI flag, e.g. `--objects-only`): stream the cell documents, skip
   `build_cells` and the report. Saves the full build on every re-run inside a `.nec`'s validity.
3. **Report the build's peak RSS** (`resource.getrusage(RUSAGE_SELF).ru_maxrss`, kB on Linux)
   in the `observe` summary / CLI JSON, and on a helper SIGKILL read the cgroup's
   `memory.events` (`oom_kill`), `memory.peak`, `memory.max` so the cause is on the row. Fleet
   dashboards key on `rss_peak_mb`, `tiled`.
