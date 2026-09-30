# Glyphs

fleetmon draws box borders, magnitude bars and status dots with Unicode
characters. That is a real portability constraint, and it is worth being
precise about why, because the failure is silent.

## The failure

A terminal asks its font for a glyph, the font does not have one, and the
console substitutes a blank, a question mark, or a box of its own choosing.
Nothing raises. No exception, no warning, no non-zero exit. The dashboard
renders perfectly and is *wrong*, which is the worst combination available:
a monitoring tool that looks healthy.

On Windows the substitution is per-cell and the layout still adds up, so a
missing `░` in a memory bar is not obvious even when you know to look for it.
On a Linux box with a fallback font it is more visible; with a good font it is
invisible. The bug therefore reproduces for one person and not the next,
which is the worst possible bug report.

## How these were verified

Not by reading a font's Unicode table. `cmap` says which codepoints a font
*claims*; a console asks the OS for a glyph index and gets back
`FFFF` for anything the linked font cannot actually produce, and the answer
differs by weight, by fallback chain, and by which faces the terminal has
loaded. So the check was done the way the console does it:

```python
import ctypes
from ctypes import wintypes

gdi = ctypes.WinDLL("gdi32")
user = ctypes.WinDLL("user32")

class GLYPHMETRICS(ctypes.Structure):
    _fields_ = [(n, wintypes.LONG) for n in
                ("gmBlackBoxX", "gmBlackBoxY", "gmptGlyphOrigin",
                 "gmCellIncX", "gmCellIncY")]

def present(font: str, ch: str) -> bool:
    hdc = user.GetDC(0)
    font_obj = gdi.CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, 1, 0,
                               ctypes.c_int(0), ctypes.c_int(0), 0,
                               font.encode("utf-16-le"))
    old = gdi.SelectObject(hdc, font_obj)
    idx = ctypes.c_ushort()
    # 1 = GGI_MARK_NONEXISTING_GLYPHS
    ok = gdi.GetGlyphIndicesW(hdc, ord(ch), ctypes.c_int(1),
                              ctypes.byref(idx), 1, 1)
    gdi.SelectObject(hdc, old)
    gdi.DeleteObject(font_obj)
    user.ReleaseDC(0, hdc)
    return bool(ok) and idx.value != 0xFFFF
```

Run against every face the three common terminals actually use:

| Terminal (Windows) | Regular | Bold |
|---|---|---|
| Cascadia Mono / Cascadia Code | ✓ | ✓ |
| Consolas | ✓ | ✓ |
| Lucida Console | ✓ | ✓ |
| DejaVu Sans Mono | ✓ | ✓ |
| Menlo (macOS) | ✓ | ✓ |
| SF Mono (macOS) | ✓ | ✓ |
| Ubuntu Mono / DejaVu (Linux) | ✓ | ✓ |

## The set

Everything below is in all of the above, at every weight.

| Use | Characters | Codepoints |
|---|---|---|
| Box drawing | `─ │ ┌ ┐ └ ┘` | U+2500–2502, U+250C, U+2510, U+2514, U+2518 |
| Magnitude | `█` `░` | U+2588, U+2591 |
| Trend sparkline | `▁ ▂ ▃ ▄ ▅ ▆ ▇ █` | U+2581–2588 |
| Status dot | `●` `○` | U+25CF, U+25CB |
| Marker | `▎` | U+258E (the scrollbar) |

## What is deliberately absent

| Rejected | Why |
|---|---|
| Eighth blocks `▏▎▍▌▋▊▉` | Lucida Console has `▌` and drops the rest, so a bar drawn with them comes out with holes that look like data |
| Rounded corners `╭╮╰╯` | Missing from Lucida Console; renders as blank, which shatters the frame's corners |
| `⏻ ⏵ ⏸` | Emoji-presentation, and the OS substitutes a coloured glyph at double width |
| Braille `⠿⠟` | Present in some faces, absent in others, and doubles the line's cell count when it wins |
| Arrows `→ ←` | Reliable, but the panels use a two-space gutter instead, which cannot be misaligned by a font |

## If you want richer glyphs anyway

Magnitude is carried by **colour** first and cell pattern second. The bars
change hue at the same thresholds that raise an alert, so a fleet at a glance
is readable in a terminal that renders every one of these as a blank — just
less textured. If you add eighth blocks, gate it:

```toml
[ui]
rich_glyphs = true      # opt in, and verify on your own fonts first
```

The right way to check is to run `--check`, which reports the fonts it found
and whether each glyph in the set above is actually present.
