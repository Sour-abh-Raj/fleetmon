"""Responsive layout.

A fixed-width dashboard is a dashboard that is wrong on somebody's screen. The
usual failure is not "too wide" -- it is "too wide by 3 columns", so borders
that lined up at 132 do not line up at 129, and the result reads as corruption
rather than as a window that was dragged.

The approach here is a **priority ladder** rather than a grid. Every panel
declares the width and height it needs to be legible, and the solver assigns
space from the most important panel down. A panel that cannot be given its
minimum is dropped with a reason rather than rendered as a broken sliver,
because "SYSTEM" at 20 columns is useful and a truncated box is not.

Two rules keep the renderer simple enough to be obviously correct:

* **Regions never overlap.** The header and footer rows are reserved before
  solving, so the canvas is a pure concatenation rather than a composite. That
  is what lets the whole renderer be plain string slicing -- ANSI escapes
  survive, which matters enormously when the entire visual language of the
  program is colour.
* **Panels are told their exact box** and return lines padded to it. Nothing
  downstream has to guess where a border belongs.

Below ``wide_at`` the ladder collapses to a single column, which is what makes
the layout usable in a quarter-screen tmux pane.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .console import pad, wlen


@dataclass(frozen=True)
class Region:
    """A rectangle in the terminal grid, absolute (header already reserved)."""
    x: int
    y: int
    w: int
    h: int

    @property
    def inner(self) -> int:
        """Usable width once the box borders and padding are removed."""
        return max(0, self.w - 4)

    @property
    def rows(self) -> int:
        """Usable height once the top and bottom borders are removed."""
        return max(0, self.h - 2)


@dataclass
class Placement:
    name: str
    region: Region | None
    reason: str = ""


@dataclass
class Node:
    """A panel's request for space.

    ``natural`` is a callable returning how many rows the panel would like,
    given the current data. The solver uses it so a two-row attention list does
    not claim a fourteen-row box. ``fill`` marks the panel that absorbs the
    rail's leftover space, which is what stops the left column ending in a void.
    """
    name: str
    min_w: int = 40
    min_h: int = 3
    want_h: int = 0              # fixed height, or 0 to size from `natural`
    natural: object = None       # () -> int
    fill: bool = False           # absorbs leftover space in its column
    weight: float = 1.0          # higher wins contested space when stacking
    side: str = "main"           # "rail" | "main" in wide mode
    key: str = ""                # hotkey, shown in the footer


@dataclass
class Layout:
    w: int
    h: int
    wide: bool
    placements: dict[str, Placement] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    def region(self, name: str) -> Region | None:
        p = self.placements.get(name)
        return p.region if p else None

    def dropped(self) -> list[Placement]:
        return [p for p in self.placements.values() if p.region is None]


class Solver:
    """Assigns terminal space to panels by priority.

    Wide mode puts the wide-and-short panels (system vitals, the server list)
    in a left rail and the tall one (the agent table) to the right. The split
    is proportional but clamped, because a 40-column agent table is unreadable
    while a 100-column one is mostly whitespace.

    Narrow mode stacks everything in priority order, so callers control what
    gets squeezed out simply by listing panels most-important-first.
    """

    def __init__(self, w: int, h: int, wide_at: int = 132,
                 rail: tuple[int, int] = (38, 52)):
        self.w = max(20, int(w))
        self.h = max(6, int(h))
        self.wide_at = wide_at
        self.rail_min, self.rail_max = rail

    def solve(self, nodes: list[Node], *, header: int = 2, footer: int = 1,
              force_narrow: bool = False) -> Layout:
        header = max(0, min(header, max(0, self.h - 3)))
        footer = max(0, min(footer, max(0, self.h - header - 2)))
        body_h = max(1, self.h - header - footer)
        wide = self.w >= self.wide_at and not force_narrow
        lay = Layout(w=self.w, h=self.h, wide=wide,
                     order=[n.name for n in nodes])
        if wide:
            self._split(lay, nodes, body_h, header)
        else:
            self._stack(lay, nodes, body_h, header)
        return lay

    # -- narrow -------------------------------------------------------------

    def _stack(self, lay: Layout, nodes: list[Node], body: int, top: int) -> None:
        """Single-column mode. ``nodes`` arrives in priority order, and that
        order is what survives a small window: whoever is first gets the space.
        """
        self._column(lay, nodes, body, top, x=0, w=lay.w)

    # -- wide ---------------------------------------------------------------

    def _split(self, lay: Layout, nodes: list[Node], body: int, top: int) -> None:
        rail_nodes = [n for n in nodes if n.side == "rail"]
        main_nodes = [n for n in nodes if n.side == "main"]
        if not main_nodes:
            main_nodes, rail_nodes = rail_nodes, []
        if not rail_nodes:
            self._stack(lay, main_nodes, body, top)
            lay.wide = False
            return

        lw = max(self.rail_min, min(self.rail_max, lay.w // 4))
        # Grow the rail until the main column can still hold a readable table.
        while lw + 44 < lay.w and lw < self.rail_max:
            lw += 2
        rw = lay.w - lw - 1

        self._column(lay, rail_nodes, body, top, x=0, w=lw)
        self._column(lay, main_nodes, body, top, x=lw + 1, w=rw)

    # -- shared column packing ----------------------------------------------

    def _column(self, lay: Layout, nodes: list[Node], body: int, top: int,
                *, x: int, w: int) -> None:
        """Fill exactly ``body`` rows, in order, with no gaps.

        Three passes, in this order, because each one only makes sense once
        the previous is settled: give every panel what it asked for, shave the
        flexible ones back if the column overflows, then hand out whatever is
        left to the panels that can use it. Panels render their full region, so
        getting this right is what makes a ragged rail look deliberate rather
        than broken.
        """
        heights: list[list] = []
        for n in nodes:
            left = (top + body) - (top + sum(h for _, h in heights))
            heights.append([n, max(0, min(self._height(n, left), left))])

        # A panel that cannot be placed must release its rows *before* the
        # arithmetic, not after. Holding them until the placement loop means
        # the sum says the column is full while five rows render as a void --
        # the space is accounted for and simultaneously unused.
        for entry in heights:
            if w < entry[0].min_w or entry[1] < entry[0].min_h:
                entry[1] = 0

        overflow = sum(h for _, h in heights) - body
        for i in range(len(heights) - 1, -1, -1):
            if overflow <= 0:
                break
            n, h = heights[i]
            if h > n.min_h:
                take = min(overflow, h - n.min_h)
                heights[i][1] = h - take
                overflow -= take

        slack = body - sum(h for _, h in heights)
        if slack > 0:
            # Share the slack out in proportion to what each filler already
            # needs. Splitting it evenly instead would grow an empty panel
            # just as fast as the busy one beside it, which is the opposite of
            # what the reader came for.
            #
            # Only panels that will actually be placed may absorb it. Handing
            # space to a panel that is then dropped for want of width leaves a
            # void in the middle of the frame, and a hole in a dashboard reads
            # as a rendering fault even when it is only a width calculation.
            idxs = [i for i, (n, _) in enumerate(heights)
                    if n.fill and w >= n.min_w and heights[i][1] >= n.min_h]
            if not idxs:
                idxs = [i for i, (n, _) in enumerate(heights)
                        if w >= n.min_w and heights[i][1] >= n.min_h]
            if idxs:
                basis = {i: max(1, heights[i][1]) for i in idxs}
                total = sum(basis.values())
                given = 0
                for i in idxs:
                    share = (slack * basis[i]) // total if i != idxs[-1] else \
                        slack - given
                    heights[i][1] += share
                    given += share
            # If nothing fits, the slack is unavoidable. The frame keeps its
            # full height and the caller reports the drops in the footer; the
            # alternative -- a short frame -- is worse, because the terminal
            # scrolls whatever used to be on screen.

        y = top
        for n, h in heights:
            # Width is checked first because it is the actionable half of the
            # diagnosis: "needs 44 cols" tells the reader their window is
            # narrow, where "no room" only tells them something was dropped.
            if w < n.min_w:
                lay.placements[n.name] = Placement(n.name, None,
                                                   f"needs {n.min_w} cols")
            elif h < n.min_h:
                lay.placements[n.name] = Placement(n.name, None, "no room")
            else:
                lay.placements[n.name] = Placement(n.name, Region(x, y, w, h))
            y += h

    @staticmethod
    def _height(n: Node, available: int) -> int:
        """How many rows this panel wants, given ``available``.

        A panel's estimate already includes its two border rows, so the number
        returned here is the whole box, not the body. Keeping that accounting
        in one place is what stops the classic off-by-two where every panel is
        two rows taller than the content that justifies it.
        """
        if n.want_h:
            return max(n.min_h, min(n.want_h, available))
        if n.natural is not None:
            try:
                want = int(n.natural())
            except Exception:
                want = n.min_h
            # A panel may ask for more room than exists; clamping here rather
            # than at draw time keeps the arithmetic honest.
            return max(n.min_h, min(want, available))
        return available


# --- rendering ---------------------------------------------------------------

def render(layout: Layout, builders: dict, *, header: list[str] | None = None,
           footer: list[str] | None = None, scrollbar: str = "") -> list[str]:
    """Materialise a layout into exactly ``layout.h`` terminal lines.

    Every row is padded to the full width so the frame never reflows when a
    panel's content shrinks -- a dashboard whose left edge jitters every second
    is unreadable no matter how good the colours are.
    """
    rows: list[list[str]] = [[" "] * layout.w for _ in range(layout.h)]

    for name in layout.order:
        p = layout.placements.get(name)
        if p is None or p.region is None:
            continue
        build = builders.get(name)
        if build is None:
            continue
        try:
            lines = build(p.region)
        except Exception as exc:                 # a broken panel must not
            lines = [f"[{name}: {type(exc).__name__}]"]   # kill the frame
        for i, line in enumerate(lines[:p.region.h]):
            y = p.region.y + i
            if 0 <= y < layout.h:
                _put(rows[y], line, p.region.x, p.region.w)

    if header:
        for i, line in enumerate(header[:2]):
            if 0 <= i < layout.h:
                _put(rows[i], line, 0, layout.w)
    if footer:
        for i, line in enumerate(footer):
            y = layout.h - len(footer) + i
            if 0 <= y < layout.h:
                _put(rows[y], line, 0, layout.w)

    if scrollbar:
        col = layout.w - 1
        for i, ch in enumerate(scrollbar[:layout.h]):
            if 0 <= i < layout.h and ch:
                rows[i][col] = ch

    return [pad("".join(r), layout.w).rstrip() for r in rows]


def _put(row: list[str], line: str, x: int, width: int) -> None:
    """Write one line into a canvas row at column ``x``, clipped to ``width``.

    Clipping has to respect escape sequences, which occupy bytes but no cells.
    ``clip`` already does that; this only places the result.
    """
    from .console import clip
    text = clip(line, width)
    plain_cols = _cols(text)
    for i, cell in enumerate(plain_cols):
        if 0 <= x + i < len(row):
            row[x + i] = cell


def _cols(line: str) -> list[str]:
    """Split into display cells, keeping each escape with the cell it colours."""
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    cells: list[str] = []
    pending = ""
    i, n = 0, len(line)
    while i < n:
        m = ansi.match(line, i)
        if m:
            pending += m.group()
            i = m.end()
            continue
        cells.append(pending + line[i])
        pending = ""
        i += 1
    return cells
