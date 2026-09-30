"""Width-aware ANSI rendering primitives.

Everything here returns plain ``str``. There is no cursor addressing and no
global state beyond the colour constants, which keeps the whole rendering
layer testable by comparing strings -- no terminal required.

The central problem this module exists to solve is that ANSI escapes have
*zero* display width but occupy real bytes. A layout that measures ``len()``
drifts by two columns per colour change, which is how a dashboard ends up
with ragged borders that look like a bug in the dashboard.
"""

from __future__ import annotations

import os
import re
import sys

CSI = "\x1b["
RESET = f"{CSI}0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

BOLD = f"{CSI}1m"
DIM = f"{CSI}2m"
ITAL = f"{CSI}3m"
UND = f"{CSI}4m"
REV = f"{CSI}7m"

# --- palette -----------------------------------------------------------------
# btop's ramp: cool green -> lime -> amber -> ember -> red. Chosen because the
# hue advances monotonically, so a glance at hue alone ranks severity even in
# a crowded panel.
RAMP = (46, 47, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196)

C_FRAME = 238
C_TITLE = 51
C_LABEL = 245
C_TEXT = 252
C_DIMTX = 243
C_OK = 47
C_LIVE = 82
C_WARN = 220
C_HOT = 208
C_BAD = 196
C_INFO = 75
C_MAG = 207
C_PURP = 141
C_CYAN = 44
C_GOLD = 178


def fg(c: int) -> str:
    return f"{CSI}38;5;{c}m"


def bg(c: int) -> str:
    return f"{CSI}48;5;{c}m"


def vlen(s: str) -> int:
    """Display width: the string with every escape sequence removed."""
    return len(ANSI_RE.sub("", s))


def strip(s: str) -> str:
    return ANSI_RE.sub("", s)


def decolor(frame: str) -> str:
    """A frame with every escape sequence removed.

    For pipes, files and tests. ``--once`` and ``--collect --raw`` both end up
    here, and a redirected frame full of escape codes is unreadable in a log
    and mis-measured by anything counting characters.

    Stripping the finished frame is the only approach that covers ``BOLD`` and
    ``RESET`` too: those are module-level constants, so a switch consulted by
    ``fg()`` would leave half the escapes in place.
    """
    return ANSI_RE.sub("", frame)


def _wcwidth(ch: str) -> int:
    """Approximate terminal cell width of a character.

    East Asian Wide/Fullwidth and emoji are double-width; combining marks are
    zero-width. Python has no builtin, and pulling in ``wcwidth`` for a
    dashboard that mostly prints ASCII would be a heavy dependency for a rare
    case -- but getting it wrong is a 1-column drift per CJK agent name, so the
    ranges are worth the dozen lines.
    """
    o = ord(ch)
    if o == 0:
        return 0
    if o < 0x0300:                      # fast path: all Latin/ASCII below here
        return 1
    if 0x0300 <= o <= 0x036F:            # combining diacriticals
        return 0
    if 0x200B <= o <= 0x200F:            # zero-width space .. RLM
        return 0
    if o in (0xFEFF,):                   # BOM
        return 0
    for lo, hi in (
        (0x1100, 0x115F), (0x2E80, 0x303E), (0x3041, 0x33FF),
        (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
        (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE6F),
        (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
        (0x1F300, 0x1F64F), (0x1F900, 0x1F9FF), (0x1F680, 0x1F6FF),
        (0x20000, 0x2FFFD), (0x30000, 0x3FFFD),
    ):
        if lo <= o <= hi:
            return 2
    return 1


def wlen(s: str) -> int:
    """Display width honouring double-width characters."""
    return sum(_wcwidth(c) for c in strip(s))


def ell(text: object, width: int) -> str:
    """Fit to ``width``, marking that something was dropped.

    A format spec like ``{x:<9}`` pads but never truncates, so one long model
    name silently pushes every column to its right out of alignment. Cutting
    mid-word with no marker is worse: "2 claude working, not attr" reads as a
    complete thought when it is half of one.
    """
    text = str(text)
    if width <= 0:
        return ""
    if wlen(text) <= width:
        return text
    if width == 1:
        return "…"
    out, used = [], 0
    for ch in text:
        cw = _wcwidth(ch)
        if used + cw > width - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def clip(s: str, width: int) -> str:
    """Hard-truncate to ``width`` cells, preserving escapes.

    Colour changes inside the discarded tail are still reset at the boundary,
    otherwise a clipped ``fg(C_BAD)`` leaks its colour into the rest of the
    line -- the classic "one red character poisons the whole row" bug.
    """
    if width <= 0:
        return ""
    if vlen(s) <= width:
        return s
    out, used, i, n = [], 0, 0, len(s)
    while i < n and used < width:
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        ch = s[i]
        i += 1
        cw = _wcwidth(ch)
        if used + cw > width:
            break
        out.append(ch)
        used += cw
    return "".join(out) + RESET


def pad(s: str, width: int, align: str = "<") -> str:
    """Pad to ``width`` cells, accounting for embedded colour."""
    gap = width - wlen(s)
    if gap <= 0:
        return s
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def truncate_cells(s: str, width: int, marker: str = "…") -> str:
    """``ell`` for already-coloured strings, keeping the trailing reset."""
    if width <= 0:
        return ""
    if wlen(s) <= width:
        return s
    plain = strip(s)
    head = ell(plain, width)
    # Re-apply the colour of the final visible character so the ellipsis does
    # not silently drop the panel's accent colour mid-row.
    last = None
    for m in re.finditer(r"\x1b\[([0-9;]*)m", s):
        last = m.group(1)
    tail = f"{CSI}{last}m" if last and last != "0" else ""
    return head + tail


# --- meters ------------------------------------------------------------------

def ramp_at(frac: float) -> int:
    """Colour for a 0..1 magnitude, sampled from :data:`RAMP`."""
    frac = 0.0 if frac != frac else max(0.0, min(1.0, frac))
    return RAMP[min(len(RAMP) - 1, int(frac * len(RAMP)))]


def ramp(frac: float, lo: int = 0, hi: int | None = None) -> int:
    """ramp_at() rescaled into a colour range, for panels with their own scale."""
    if frac != frac:
        frac = 0.0
    frac = max(0.0, min(1.0, frac))
    if hi is None:
        hi = len(RAMP) - 1
    return RAMP[lo + min(hi - lo, int(frac * (hi - lo + 1)))]


# Deliberately conservative glyph set. Anything richer (sub-eighth blocks,
# rounded corners, the dashed rule) is absent from at least one of Consolas,
# Lucida Console and Cascadia Mono, and a console renders a missing glyph as a
# blank with no error. Verified with GDI GetGlyphIndicesW -- see docs/glyphs.md.
FULL = "█"        # FULL BLOCK
HALF = "▄"        # LOWER HALF BLOCK
LIGHT = "░"       # LIGHT SHADE
HEAVY = "▌"       # LEFT HALF BLOCK
RULE = "─"        # BOX DRAWINGS LIGHT HORIZONTAL
VLINE = "│"       # BOX DRAWINGS LIGHT VERTICAL

BAR_STYLES = {
    "block": (FULL, " "),
    "shade": (HEAVY, LIGHT),
    "thin": ("▬", "·"),
    "ascii": ("#", "."),
}


def bar(frac: float, width: int, *, style: str = "block", color: int | None = None,
        track: int = C_FRAME, invert: bool = False) -> str:
    """A horizontal meter.

    ``frac`` is clamped rather than raising: a collector returning a garbage
    ratio should degrade the picture, not take down the frame.
    """
    if width <= 0:
        return ""
    try:
        frac = float(frac)
    except (TypeError, ValueError):
        frac = 0.0
    frac = 0.0 if frac != frac else max(0.0, min(1.0, frac))
    if invert:
        frac = 1.0 - frac
    on, off = BAR_STYLES.get(style, BAR_STYLES["block"])
    total = int(round(frac * width))
    col = ramp_at(frac) if color is None else color
    return (f"{fg(col)}{on * total}{RESET}" if total else "") + \
           f"{fg(track)}{off * (width - total)}{RESET}"


def spark(values, width: int, *, vmax: float | None = None, color: int | None = None,
          style: str = "block") -> str:
    """A trend line from a series.

    Magnitude is carried by *colour*, not by which block-drawing character is
    used. That is deliberate: the partial-height blocks are exactly the glyphs
    missing from half the common console fonts, and a sparkline drawn in
    glyphs degrades to noise on exactly the machines that need it most.
    """
    vals = [0.0 if v != v else float(v) for v in values][-width:]
    if not vals or width <= 0:
        return f"{fg(C_DIMTX)}{LIGHT * min(width, max(0, len(vals)))}{RESET}"
    top = vmax if vmax is not None else max(vals) or 1.0
    if top <= 0:
        top = 1.0
    out = []
    for v in vals:
        f = max(0.0, min(1.0, v / top))
        if color is not None:
            col = color
        else:
            col = C_DIMTX if f <= 0 else ramp_at(f)
        out.append(f"{fg(col)}{HALF if f > 0 else LIGHT}{RESET}")
    pad = width - len(out)
    if pad > 0:
        out.insert(0, " " * 0)
        out = [" "] * 0 + out
        out = ([f"{fg(C_DIMTX)}{LIGHT}{RESET}"] * pad) + out
    return "".join(out)


def cores_strip(values, width: int, *, show_count: bool = True) -> str:
    """One cell per CPU core.

    A single average hides the thing that matters on a busy box: one core
    pinned at 100% while the total reads a comfortable 25%.
    """
    if not values:
        return ""
    vals = list(values)
    shown = vals[:width]
    out = []
    for v in shown:
        f = max(0.0, min(1.0, float(v) / 100.0))
        out.append(LIGHT if v <= 0 else fg(ramp_at(f)) + FULL + RESET)
    strip_s = "".join(out)
    if show_count and len(vals) > len(shown):
        strip_s += fg(C_FRAME) + f"+{len(vals) - len(shown)}" + RESET
    return strip_s


# --- boxes -------------------------------------------------------------------

def box(title: str, body: list[str], width: int, *, right: str = "",
        accent: int = C_TITLE, frame: int = C_FRAME) -> list[str]:
    """A titled panel. ``body`` rows are clipped, not truncated silently.

    Every returned line is exactly ``width`` cells wide. That exactness is the
    whole contract: the layout solver places panels side by side on integer
    column boundaries, so a title line one cell short shows up as a column of
    black between two boxes and reads as a rendering fault.
    """
    if width < 8:
        return []
    inner = width - 4                      # between the two border columns
    head = f" {fg(accent)}{BOLD}{title}{RESET}" if title else ""
    if right:
        gap = inner - wlen(title) - wlen(right) - 2
        head += " " * max(1, gap) + fg(C_DIMTX) + right + RESET
    # One cell short of the full inner width, so there is always at least one
    # rule character between the last header character and the corner. A title
    # butted straight into the border reads as a missing glyph.
    head = clip(head, max(0, width - 5))
    dash = max(1, width - 3 - wlen(head))
    top = f"{fg(frame)}┌─{RESET}{head}{fg(frame)}{'─' * dash}┐{RESET}"
    mid = f"{fg(frame)}│{RESET} {{}} {fg(frame)}│{RESET}"
    bot = f"{fg(frame)}└{'─' * (width - 2)}┘{RESET}"
    lines = [top]
    for row in body:
        lines.append(mid.format(pad(clip(row, inner), inner)))
    lines.append(bot)
    return lines


def rule(width: int, title: str = "", *, color: int = C_FRAME) -> str:
    if width <= 0:
        return ""
    if not title:
        return fg(color) + RULE * width + RESET
    t = f" {title} "
    left = 2
    right = max(0, width - left - wlen(t))
    return fg(color) + RULE * left + RESET + fg(C_DIMTX) + t + RESET + \
        fg(color) + RULE * right + RESET


def side_by_side(cols: list[list[str]], gap: int = 1,
                 widths: list[int] | None = None, height: int | None = None) -> list[str]:
    """Join column blocks horizontally, equalising height.

    Padding the short column with blank lines is what makes a layout look
    broken rather than designed, so when ``height`` is given every column is
    padded or truncated to exactly that many rows.
    """
    if not cols:
        return []
    widths = widths or [max((vlen(r) for r in c), default=0) for c in cols]
    height = height or max((len(c) for c in cols), default=0)
    gapstr = " " * gap
    out = []
    for i in range(height):
        parts = []
        for c, w in zip(cols, widths):
            row = c[i] if i < len(c) else ""
            parts.append(pad(row, w))
        out.append(gapstr.join(parts).rstrip())
    return out


def join_rows(rows: list[str]) -> str:
    return "".join(rows)


# --- capability --------------------------------------------------------------

def supports_truecolor(stream=None) -> bool:
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    term = os.environ.get("TERM", "")
    return "256" in term or "kitty" in term or "direct" in term


def strip_env() -> None:
    """Force UTF-8 on stdout/stderr.

    Python picks the stream encoding from the CRT locale at startup. On a real
    console that is the console itself and Unicode passes straight through;
    the moment a frame is piped or redirected -- ``fleetmon > out.txt``, cron,
    ssh -- it falls back to cp1252 and dies on the first box character.
    """
    for name in ("stdout", "stderr"):
        try:
            getattr(sys, name).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def enable_vt(stream=None) -> bool:
    """Turn on ANSI interpretation. Returns True if VT is active."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
            h = k.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)  # VIRTUAL_TERMINAL
            return True
        except Exception:
            return False
    return sys.stdout.isatty()


def set_font() -> bool:
    """Ask conhost for a font with box-drawing coverage.

    Only meaningful when we are *not* already inside Windows Terminal, which
    renders with its own font and would ignore this.
    """
    if os.name != "nt" or os.environ.get("WT_SESSION"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.GetStdHandle(-11)
        u.SetConsoleOutputCP(65001)
        for family in ("Cascadia Mono", "Cascadia Code", "Consolas", "Lucida Console"):
            class F(ctypes.Structure):
                _fields_ = [("dwSize", ctypes.c_short),
                            ("dwFontFamily", ctypes.c_int),
                            ("dwFontWeight", ctypes.c_int),
                            ("cchFontName", ctypes.c_wchar * 32),
                            ("dwFontIndex", ctypes.c_short),
                            ("dwFlags", ctypes.c_int)]
            f = F()
            f.cchFontName = family
            if u.SetCurrentConsoleFontEx(h, True, ctypes.byref(f)):
                return True
        return False
    except Exception:
        return False
