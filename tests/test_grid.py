import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vtcseamless.bevdirect import cells_for, cell_bbox, cell_of, pad5


def test_cells_for_matches_server_rule():
    # a client tile of 0.02° starting off-grid touches 2×2 cells
    assert len(cells_for(15.075, 47.055, 15.095, 47.075)) == 4
    # exactly grid-aligned → 1 cell
    assert cells_for(15.08, 47.06, 15.10, 47.08) == [(754, 2353)]
    w, s, e, n = cell_bbox(*cell_of(15.0812, 47.0611))
    assert w <= 15.0812 <= e and s <= 47.0611 <= n


def test_documented_domain_cells():
    # the operator baseline of one KG was built from cells x 753–757 × y 2354–2357 for
    # input_bbox 15.0712,47.0902,15.1517,47.1553 — the grid helper must give the same set
    cs = cells_for(15.0712, 47.0902, 15.1517, 47.1553)
    assert {c[0] for c in cs} == set(range(753, 758)) and {c[1] for c in cs} == set(range(2354, 2358))
    assert len(cs) == 20


def test_cell_of_grid_line_is_single_cell():
    # bevdirect-serve < v0.3.6 bug: 15.54 / 0.02 = 776.999… → floor 776, so an aligned cell
    # resolved to two cells; every grid line (and its 7-decimal rounding) must map to its own ix
    import math
    for ix in range(-9000, 9001):
        for lon in (ix * 0.02, round(ix * 0.02, 7)):
            assert cell_of(lon, lon)[0] == ix, (ix, lon)
    assert cells_for(15.54, 48.40, 15.56, 48.42) == [(777, 2420)]
    for ix in range(-9000, 9000):
        w, s, e, n = cell_bbox(ix, ix)
        assert cells_for(w, s, e, n) == [(ix, ix)]
    # a point just inside the cell does not creep into the next one
    assert cell_of(15.56 - 1e-9, 48.42 - 1e-9) == (777, 2420)


def test_pad5():
    assert pad5("1503") == "01503" and pad5("63330") == "63330" and pad5("abc") == "abc"
