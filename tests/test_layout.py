"""Layout tests: the promise that a frame always fits the terminal.

The user-visible claim is "resize the window and it just works". Every one of
these assertions is a way that claim breaks, so they are worth being pedantic
about.
"""

from __future__ import annotations

import pytest

from fleetmon.console import box, clip, ell, pad, strip, wlen
from fleetmon.layout import Node, Region, Solver, render


def nodes():
    """A miniature panel set with the same shape as the real one."""
    return [
        Node(name="attn", min_w=20, min_h=3, side="rail", weight=9.0,
             natural=lambda: 4),
        Node(name="sys", min_w=24, min_h=4, side="rail", weight=8.0,
             natural=lambda: 6),
        Node(name="svc", min_w=24, min_h=3, side="rail", weight=7.0,
             natural=lambda: 5),
        Node(name="tmux", min_w=24, min_h=3, side="rail", weight=5.0,
             natural=lambda: 5, fill=True),
        Node(name="agents", min_w=40, min_h=5, side="main", weight=10.0,
             natural=lambda: 8, fill=True),
        Node(name="sessions", min_w=40, min_h=4, side="main", weight=4.0,
             natural=lambda: 5, fill=True),
    ]


SIZES = [(200, 60), (160, 44), (132, 40), (120, 30), (100, 24), (80, 20),
         (60, 16), (40, 12), (30, 10), (20, 8)]


@pytest.mark.parametrize("w,h", SIZES)
def test_frame_is_exactly_the_terminal(w, h):
    lay = Solver(w, h).solve(nodes(), header=2, footer=1)
    out = render(lay, {}, header=["", ""], footer=[""])
    assert len(out) == h
    for line in out:
        assert wlen(line) <= w


@pytest.mark.parametrize("w,h", SIZES)
def test_no_column_ends_in_a_void(w, h):
    """Every placed panel's region must abut the next one's, with no gap.

    A hole in the middle of a dashboard reads as a rendering bug even when it
    is only a rounding artefact, so the invariant is asserted rather than
    eyeballed.
    """
    ns = nodes()
    lay = Solver(w, h).solve(ns, header=2, footer=1)
    for side in ("rail", "main"):
        col = [lay.placements[n.name].region for n in ns
               if n.side == side and lay.placements[n.name].region]
        col.sort(key=lambda r: r.y)
        for a, b in zip(col, col[1:]):
            assert b.y == a.y + a.h, f"gap in {side} column at y={a.y + a.h}"
    placed = [p.region for p in lay.placements.values() if p.region]
    assert placed, "a 20x8 terminal should still fit one panel"


@pytest.mark.parametrize("w,h", SIZES)
def test_regions_stay_inside_the_canvas(w, h):
    lay = Solver(w, h).solve(nodes(), header=2, footer=1)
    for p in lay.placements.values():
        r = p.region
        if r is None:
            continue
        assert r.y >= 0 and r.y + r.h <= h, f"{p.name} overflows vertically"
        assert r.x >= 0 and r.x + r.w <= w, f"{p.name} overflows horizontally"


def test_wide_mode_splits_into_two_columns():
    lay = Solver(200, 50).solve(nodes(), header=2, footer=1)
    assert lay.wide
    rail = [n for n in nodes() if n.side == "rail"]
    xs = {lay.placements[n.name].region.x for n in rail
          if lay.placements[n.name].region}
    assert len(xs) == 1, "rail panels must share one x"


def test_narrow_mode_stacks_in_priority_order():
    ns = nodes()
    ns[0].name = "attention"                # first in the list == highest
    lay = Solver(40, 60).solve(ns, header=2, footer=1)
    assert not lay.wide
    first = min((p.region for p in lay.placements.values() if p.region),
                key=lambda r: r.y)
    assert first.y == 2, "the first panel must sit directly under the header"


def test_attention_survives_the_smallest_window():
    """The one panel that must never be squeezed out is the one that asks."""
    ns = [Node(name="attn", min_w=20, min_h=3, natural=lambda: 4),
          Node(name="big", min_w=40, min_h=20, natural=lambda: 40, fill=True)]
    lay = Solver(46, 14).solve(ns, header=2, footer=1)
    assert lay.placements["attn"].region is not None
    assert lay.placements["attn"].region.y == 2


def test_a_broken_panel_does_not_take_down_the_frame():
    ns = [Node(name="ok", min_w=20, min_h=3, natural=lambda: 4),
          Node(name="boom", min_w=20, min_h=3, natural=lambda: 4)]
    lay = Solver(80, 12).solve(ns, header=2, footer=1)

    def boom(region):
        raise RuntimeError("collector exploded")

    out = render(lay, {"ok": lambda r: ["fine"] * r.rows, "boom": boom})
    assert any("boom" in strip(l) for l in out)
    assert any("fine" in strip(l) for l in out)


def test_fill_distributes_slack_in_proportion_to_demand():
    a = Node(name="a", min_h=2, natural=lambda: 10, fill=True)
    b = Node(name="b", min_h=2, natural=lambda: 2, fill=True)
    lay = Solver(80, 30).solve([a, b], header=0, footer=0)
    ha = lay.placements["a"].region.h
    hb = lay.placements["b"].region.h
    assert ha > hb and ha + hb == 30


def test_box_lines_are_exactly_its_width():
    for width in (10, 12, 20, 40, 52, 80, 132):
        for title in ("", "SHORT", "A VERY LONG PANEL TITLE INDEED"):
            for right in ("", "1", "9999"):
                lines = box(title, ["x" * 200], width, right=right)
                assert lines, f"box({title!r}, {width}) returned nothing"
                assert all(wlen(l) == width for l in lines), \
                    f"box({title!r},{right!r},{width}) -> {[wlen(l) for l in lines]}"


def test_box_survives_a_title_wider_than_the_box():
    lines = box("AN EXTREMELY LONG TITLE", [], 12)
    assert all(wlen(l) == 12 for l in lines)


def test_ell_never_exceeds_its_width():
    for s in ("", "short", "a" * 50, "日本語のテキスト", "emoji 🚀🚀🚀"):
        for w in range(0, 12):
            assert wlen(ell(s, w)) <= max(0, w)


def test_pad_is_width_aware_through_colour():
    s = "\x1b[38;5;1mabc\x1b[0m"
    assert wlen(pad(s, 10)) == 10
    assert wlen(pad(s, 10, ">")) == 10
    assert wlen(pad(s, 1)) >= 3        # never truncates, just does not pad


def test_clip_resets_at_the_boundary():
    s = "\x1b[38;5;196m" + "x" * 50 + "\x1b[0m"
    out = clip(s, 5)
    assert wlen(out) == 5
    assert out.endswith("\x1b[0m"), "colour must not leak past the clip"


def test_region_geometry_helpers():
    r = Region(0, 2, 50, 10)
    assert r.inner == 46 and r.rows == 8
    assert Region(0, 0, 2, 1).inner == 0