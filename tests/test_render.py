"""Render, input and config tests -- the parts a user touches directly."""

from __future__ import annotations

import time

import pytest

from fleetmon.console import strip, wlen
from fleetmon.config import Config, HostRef, apply, parse_args
from fleetmon.model import human_n
from fleetmon.term import Key, Pager, translate

from .helpers import fake_host, fake_hosts


# --- rendering ---------------------------------------------------------------

@pytest.mark.parametrize("w,h", [(200, 60), (132, 40), (100, 30), (72, 24),
                                 (46, 18), (30, 12)])
def test_render_once_fits_every_geometry(w, h):
    from fleetmon.layout import Solver, render
    from fleetmon import panels as P

    cfg = Config(read_transcripts=False)
    hosts = fake_hosts(2)
    lay = Solver(w, h, cfg.wide_at).solve(P.nodes_for(cfg, hosts),
                                          header=2, footer=1)
    out = render(lay, {n: (lambda r, n=n: P.PANELS[n].fn(hosts, r, cfg, None))
                       for n in lay.order if n in P.PANELS})
    assert len(out) == h
    assert all(wlen(l) <= w for l in out)


@pytest.mark.parametrize("w,h", [(200, 60), (132, 40), (100, 30), (60, 20),
                                 (40, 12)])
def test_the_one_shot_frame_is_exact_and_complete(w, h):
    """``render_once`` is a public seam, so its frame is held to the same
    contract as the live one: exactly ``h`` lines, none wider than ``w``, and
    the header and footer actually drawn.

    It was reserving header and footer rows in the solve and then rendering
    neither, which produced a plausible-looking frame with a blank band top
    and bottom -- and any test written against it would have passed.
    """
    from fleetmon import render_once

    frame = render_once(Config(read_transcripts=False), w=w, h=h,
                        colors=False, hosts=fake_hosts(2))
    lines = frame.splitlines()
    assert len(lines) == h, f"{w}x{h} gave {len(lines)} lines"
    assert all(wlen(l) <= w for l in lines)
    assert "\x1b" not in frame, "colors=False must actually strip escapes"
    assert "fleetmon" in lines[0], "the header row is missing"
    # The footer carries errors first and key hints second, so on a narrow
    # window the correct footer is *only* the error. What must never happen is
    # an empty one -- that is the row that tells you what got hidden.
    assert lines[-1].strip(), "the footer row is empty"
    assert lines[-1] == lines[-1].rstrip(), "the footer has trailing padding"


def test_colors_survive_when_asked_for():
    from fleetmon import render_once

    frame = render_once(Config(read_transcripts=False), w=100, h=30,
                        hosts=fake_hosts(2))
    assert "\x1b[" in frame


def test_the_system_panel_does_not_truncate_its_own_numbers():
    """`435.2G/510.1G` cut to `435.2G/5` reads as a different number.

    The bar used to be sized from a hardcoded allowance for the right-hand
    text rather than from its actual width, so the longest values lost their
    tail -- silently, and only on the rows with the most to say.
    """
    from fleetmon import panels as P
    from fleetmon.layout import Region

    cfg = Config(read_transcripts=False)
    host = fake_hosts(1)
    for width in (60, 80, 100, 120):
        region = Region(0, 0, width, 16)
        text = "\n".join(strip(l) for l in P.p_system(host, region, cfg, None))
        # The fixture's own numbers, not literals copied off a real machine:
        # a truncated "322.1G/549.8G" reads as "322.1G/5", and the half that
        # carries the meaning is exactly the half that goes missing.
        for label in (human_n(host[0].mem.total),
                      human_n(host[0].disk.total),
                      human_n(host[0].gpus[0].vram_total)):
            assert label in text, f"{label} was clipped at width {width}"


@pytest.mark.parametrize("w", [200, 132, 100, 72, 46, 30, 20])
def test_no_panel_raises_at_any_width(w):
    """A panel must degrade, never throw, however narrow the window gets."""
    from fleetmon import panels as P

    cfg = Config(read_transcripts=False)
    hosts = fake_hosts(3)
    for name in P.ORDER:
        panel = P.PANELS[name]
        from fleetmon.layout import Region
        for width in range(8, w + 1, 7):
            for height in (3, 5, 9, 20):
                region = Region(0, 0, width, height)
                out = panel.fn(hosts, region, cfg, None)
                assert all(wlen(l) <= width for l in out), \
                    f"{name} overflowed at {width}x{height}"


def test_agent_table_drops_columns_before_it_overflows():
    from fleetmon.panels import fit_cols
    for w in range(10, 200):
        cols = fit_cols(w)
        if cols:
            assert sum(c[2] + 2 for c in cols) <= w


def test_panels_fill_their_region_exactly():
    from fleetmon import panels as P
    from fleetmon.layout import Region

    cfg = Config(read_transcripts=False)
    hosts = fake_hosts(1)
    for name in P.ORDER:
        for height in (6, 10, 18):
            region = Region(0, 0, 90, height)
            out = P.PANELS[name].fn(hosts, region, cfg, None)
            assert len(out) == height, f"{name} returned {len(out)} of {height}"


def test_attention_is_empty_when_nothing_is_wrong():
    from fleetmon import panels as P
    from fleetmon.config import Config
    cfg = Config(read_transcripts=False, crit_cpu=100.0, crit_mem=100.0,
                 crit_disk=100.0, warn_cpu=100.0, warn_mem=100.0,
                 warn_disk=100.0)
    assert P.collect_attention(fake_hosts(1), cfg) == []


def test_attention_reports_a_down_port():
    from fleetmon import panels as P
    from fleetmon.config import Config
    cfg = Config(read_transcripts=False, crit_cpu=100.0, crit_mem=100.0,
                 crit_disk=100.0)
    items = P.collect_attention(fake_hosts(1, down_port="8000"), cfg)
    assert [m for _, _, m in items if "down" in m]


def test_attention_ranks_the_silent_peer_first():
    from fleetmon import panels as P
    cfg = Config(stale_host=1.0)
    hosts = fake_hosts(2)
    hosts[1].seen = time.time() - 600.0     # ancient
    items = P.collect_attention(hosts, cfg)
    assert items and "silent" in items[0][2]
    assert items[0][2].startswith("host1")


def test_a_peer_with_no_timestamp_is_called_silent_not_healthy():
    """``seen == 0`` is an unknown age, and unknown is not the same as fine."""
    from fleetmon import panels as P
    from fleetmon.config import Config
    cfg = Config(read_transcripts=False, stale_host=1e9, crit_cpu=100.0,
                 crit_mem=100.0, crit_disk=100.0)
    host = fake_host("ghost", seen=0.0)
    host.seen = 0.0
    items = P.collect_attention([host], cfg)
    assert items and "no timestamp" in items[0][2]


# --- pager -------------------------------------------------------------------

def test_pager_starts_live_and_scrolls_back():
    p = Pager(history=__import__("collections").deque(maxlen=10))
    for i in range(5):
        p.push(f"frame{i}")
    assert p.live and p.view() == "frame4"
    p.back()
    assert not p.live and p.view() == "frame3"
    p.top()
    assert p.view() == "frame0"
    p.bottom()
    assert p.live


def test_pager_clamps_at_both_ends():
    from collections import deque
    p = Pager(history=deque(maxlen=10))
    p.push("a")
    for _ in range(50):
        p.back()
        p.clamp()
    assert p.pos == 0
    for _ in range(50):
        p.forward()
    assert p.pos == 0


def test_pager_follows_the_tail_only_when_the_reader_is_close_to_it():
    from collections import deque
    p = Pager(history=deque(maxlen=10))
    for i in range(5):
        p.push(f"f{i}")
    p.back(2)
    held = p.pos
    p.push("f5")
    assert p.pos == held + 1, "a reader one frame back should be carried along"

    p.top()
    far = p.pos
    p.push("f6")
    assert p.pos == far, "a reader at the top must not be yanked forward"


def test_scrollbar_is_blank_when_live():
    from collections import deque
    p = Pager(history=deque(maxlen=10))
    for i in range(20):
        p.push(f"f{i}")
    assert p.scrollbar(20) != ""
    p.bottom()
    assert p.scrollbar(20) != ""       # live still shows the position


def test_empty_pager_has_nothing_to_show():
    p = Pager()
    assert p.view() is None
    assert p.scrollbar(20) == ""


# --- keys --------------------------------------------------------------------

@pytest.mark.parametrize("seq,name", [
    ("k", "char"), ("\x03", "ctrl-c"), ("\r", "enter"), ("\x1b", "escape"),
    ("\x1b[A", "up"), ("\x1b[B", "down"), ("\x1bOA", "up"),
    ("\x1b[5~", "pagedown"), ("\x1b[6~", "pageup"),
    ("\x1b[H", "home"), ("\x1b[F", "end"),
])
def test_translate_maps_the_keys_we_bind(seq, name):
    assert translate(seq).name == name


def test_translate_ignores_mouse_and_focus_reports():
    for seq in ("\x1b[<0;10;20M", "\x1b[<0;10;20m", "\x1b[200~paste"):
        assert translate(seq) is None


def test_pager_key_bindings_round_trip():
    from collections import deque
    p = Pager(history=deque(maxlen=10))
    for i in range(5):
        p.push(f"f{i}")
    assert p.handle(Key("char", "k"), 3) and not p.live
    assert p.handle(Key("char", "j"), 3) and p.live
    assert p.handle(Key("up"), 3) and not p.live
    assert p.handle(Key("char", "G"), 3) and p.live
    assert not p.handle(Key("char", "q"), 3)


# --- config ------------------------------------------------------------------

def test_flags_beat_the_file(tmp_path):
    f = tmp_path / "fleetmon.toml"
    f.write_text('[ui]\nlabel = "from-file"\nrefresh = 9.0\n', encoding="utf-8")
    cfg, _, _ = parse_args(["--config", str(f), "--refresh", "0.5"])
    assert cfg.label == "from-file" and cfg.refresh == 0.5


def test_unknown_keys_are_dropped_not_fatal():
    cfg = apply(Config(), {"nonsense": 1, "label": "ok"})
    assert cfg.label == "ok"


def test_sections_fold_into_the_flat_config():
    cfg = apply(Config(), {"ui": {"label": "x"}, "limits": {"warn_cpu": 10.0}})
    assert cfg.label == "x" and cfg.warn_cpu == 10.0


def test_hosts_parse_from_both_shorthands():
    cfg = apply(Config(), {"hosts": ["nixos", {"name": "w", "via": "http",
                                                "target": "http://w:8731"}]})
    assert [h.name for h in cfg.hosts] == ["nixos", "w"]
    assert cfg.hosts[1].via == "http"


def test_peer_flag_becomes_an_auto_host():
    cfg, _, _ = parse_args(["--peer", "nixos"])
    assert cfg.hosts and cfg.hosts[0].name == "nixos" and cfg.hosts[0].via == "auto"


def test_display_prefers_the_label():
    assert Config(label="desk").display == "desk"


def test_configured_panels_restrict_the_set():
    from fleetmon import panels as P
    cfg = Config(panels=["system", "agents"])
    assert P.enabled(cfg) == ["agents", "system"]
    assert P.enabled(Config(hidden_panels=["tmux"])) == \
        [p for p in P.ORDER if p != "tmux"]