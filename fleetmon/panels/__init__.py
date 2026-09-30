"""Panels: the things actually drawn.

Each panel is a builder taking a :class:`~fleetmon.layout.Region` and
returning lines that **fill it exactly**. Filling is not a nicety -- it is what
makes the column-packing solver possible. If a panel returned only the rows it
had content for, the solver would have to guess where the next one starts, and
the left rail would end in a ragged void that reads as a rendering fault.

Every panel also has a narrow form. A responsive dashboard is not one that
reflows; it is one that drops columns in priority order as width falls.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..console import (
    BOLD, C_BAD, C_CYAN, C_DIMTX, C_FRAME, C_GOLD, C_HOT, C_LABEL, C_LIVE,
    C_MAG, C_OK, C_PURP, C_TEXT, C_WARN, DIM, RESET, bar, box, clip,
    cores_strip, ell, fg, pad, ramp_at, spark, wlen,
)
from ..layout import Node, Region
from ..model import Agent, Host, Session, human_age, human_mb, human_n


# --- helpers -----------------------------------------------------------------

def dot(color: int, ch: str = "●") -> str:
    return f"{fg(color)}{ch}{RESET}"


def meter(pct: float, width: int) -> str:
    return bar(pct / 100.0, max(0, width))


def sev(pct: float, warn: float, crit: float) -> int:
    return C_BAD if pct >= crit else C_WARN if pct >= warn else C_OK


def fill(title: str, body: list[str], region: Region, right: str = "",
         accent: int = 0) -> list[str]:
    """A boxed panel padded to occupy its whole region.

    Padding the *body* rather than the output means the bottom border always
    lands on the panel's last row, so a short list is a short box in a tall
    slot instead of a tall slot with a box floating in it.
    """
    rows = max(0, region.rows)
    if rows < 2 or region.w < 8:
        return []
    body = list(body[:rows])
    if len(body) < rows:
        body.extend([""] * (rows - len(body)))
    kw = {}
    if accent:
        kw["accent"] = accent
    return box(title, body, region.w, right=right, **kw)[:region.h]


# --- SYSTEM ------------------------------------------------------------------

def _sys_rows(h: Host) -> list[tuple[str, float, float, float, str]]:
    """(label, pct, warn, crit, right-hand text) for one host's vitals."""
    return [
        ("cpu", h.cpu.pct, 78.0, 92.0,
         f"{len(h.cpu.cores) or 0}c"),
        ("mem", h.mem.pct, 85.0, 94.0,
         f"{human_n(h.mem.used)}/{human_n(h.mem.total)}"),
        ("dsk", h.disk.pct, 88.0, 95.0,
         f"{human_n(h.disk.used)}/{human_n(h.disk.total)}"),
    ]


def p_system(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    """CPU, memory, disk, network, GPU -- one block per host."""
    w = region.inner
    body = []
    show_host = len(hosts) > 1
    tagw = 11 if show_host else 0

    for h in hosts:
        c, m, d = h.cpu, h.mem, h.disk
        stale = (time.time() - h.seen) > cfg.stale_host if h.seen else False
        hostcol = C_BAD if stale else (C_CYAN if h.self_host else C_PURP)
        lead = f"{fg(hostcol)}{ell(h.display, 11):<11}{RESET}" if show_host else ""

        def row(label, pct, warn, crit, right):
            col = sev(pct, warn, crit)
            # The bar gets whatever the label, the percentage and the
            # right-hand text leave over -- measured, not assumed. Reserving a
            # fixed 20 columns for the right side truncated "435.2G/512G" to
            # "435.2G/5", which reads as a different number rather than as a
            # panel that ran out of room.
            fixed = tagw + len(label) + 1 + 5 + 1 + wlen(right) + 1
            metw = max(4, min(16, w - fixed))
            # A per-core strip replaces the average bar where it fits. One core
            # pinned at 100% behind an average reading 25% is exactly the thing
            # a single bar hides, and it is often the whole story.
            strip_ = cores_strip(c.cores, metw) if label == "cpu" else ""
            met = strip_ or meter(pct, metw)
            return clip(f"{lead}{fg(C_LABEL)}{label} {RESET}"
                        f"{fg(col)}{BOLD}{pct:5.1f}%{RESET} {met} "
                        f"{fg(C_DIMTX)}{right}{RESET}", w)

        body.append(row("cpu", c.pct, cfg.warn_cpu, cfg.crit_cpu,
                        f"L{c.load[0]:.1f}" if c.load else
                        f"{c.n or 0}c"))
        if m.total:
            body.append(row("mem", m.pct, cfg.warn_mem, cfg.crit_mem,
                            f"{human_n(m.used)}/{human_n(m.total)}"))
        if d.total:
            body.append(row("dsk", d.pct, cfg.warn_disk, cfg.crit_disk,
                            f"{human_n(d.used)}/{human_n(d.total)}"))
        for g in h.gpus[:2]:
            # Same measured-width rule as the bars above, for the same reason:
            # a hardcoded allowance clipped "2.1G/24.0G" to "2.1G/" and lost the
            # capacity, which is the number that tells you whether to worry.
            right = (f"{ell(g.name, 10)} {human_n(g.vram_used)}/"
                     f"{human_n(g.vram_total)} {g.temp}C")
            metw = max(4, min(12, w - (tagw + 4 + 5 + 1 + wlen(right) + 1)))
            body.append(clip(
                f"{lead}{fg(C_LABEL)}gpu {RESET}"
                f"{fg(ramp_at(g.pct / 100))}{BOLD}{g.pct:5.1f}%{RESET} "
                f"{meter(g.pct, metw)} {fg(C_DIMTX)}{right}{RESET}", w))
        if show_host or w >= 56:
            body.append(clip(
                f"{lead}{fg(C_DIMTX)}up {human_age(h.uptime)}  "
                f"net {human_n(h.net.rx)}/{human_n(h.net.tx)}"
                + (f"  {fg(C_BAD)}STALE {RESET}"
                   if stale else "") + RESET, w))
        if h is not hosts[-1]:
            body.append("")

    right = f"{len(hosts)} host{'s' if len(hosts) != 1 else ''}" if hosts else ""
    return fill("SYSTEM", body, region, right)


def _system_natural(hosts: list[Host], cfg) -> int:
    """Full box height: rows plus the two borders."""
    n = 2
    for i, h in enumerate(hosts):
        n += 3 + len(h.gpus[:2]) + (1 if (len(hosts) > 1) else 0)
        if i < len(hosts) - 1:
            n += 1
    return n


# --- ATTENTION ---------------------------------------------------------------

def p_attention(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    """Everything on the fleet that wants a human, and nothing that does not."""
    w = region.inner
    items = collect_attention(hosts, cfg)
    if not items:
        body = [f"{fg(C_OK)}nothing needs you{RESET}"]
    else:
        body = [f"{dot(c)} {fg(c)}{ell(msg, max(10, w - 2))}{RESET}"
                for _, c, msg in items]
    return fill("ATTENTION", body, region,
                right=str(len(items)) if items else "")


def collect_attention(hosts: list[Host], cfg) -> list[tuple[int, int, str]]:
    """Ranked ``(severity, colour, message)`` -- shared by the panel and --raw."""
    items: list[tuple[int, int, str]] = []
    for h in hosts:
        # ``seen == 0`` means the age is *unknown*, not zero. Treating a missing
        # timestamp as "collected just now" renders a peer whose clock or
        # payload failed as perfectly healthy, which is the one thing a fleet
        # view must never do -- so an unknown age is called out as such.
        if not h.seen:
            items.append((0, C_BAD, f"{h.display} silent (no timestamp)"))
        elif (fresh := time.time() - h.seen) > cfg.stale_host:
            items.append((0, C_BAD, f"{h.display} silent {human_age(fresh)}"))
        for a in h.alerts:
            items.append((1, C_WARN, f"{h.display} {a}"))
        for p in h.ports.values():
            if not p.up:
                items.append((2, C_WARN, f"{h.display} :{p.label or ''} down"))
        m, c, d = h.mem, h.cpu, h.disk
        if m.total and m.pct >= cfg.crit_mem:
            items.append((3, C_BAD, f"{h.display} mem {m.pct:.0f}%"))
        if c.pct >= cfg.crit_cpu:
            items.append((4, C_BAD, f"{h.display} cpu {c.pct:.0f}%"))
        if d.total and d.pct >= cfg.crit_disk:
            items.append((5, C_BAD, f"{h.display} disk {d.pct:.0f}%"))
        for g in h.gpus:
            if g.temp >= 85:
                items.append((6, C_HOT, f"{h.display} gpu {g.temp}C"))
    items.sort(key=lambda t: t[0])
    return items


# --- LLM ---------------------------------------------------------------------

def p_llm(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    """Inference servers across the fleet.

    An empty panel is the correct answer on a machine that does not host
    inference. Showing four "DOWN" rows for ports that were never meant to run
    here is how a dashboard teaches its reader to ignore it.
    """
    w = region.inner
    rows = []
    for h in hosts:
        for port, p in h.ports.items():
            rows.append((h, int(port), p))
    rows.sort(key=lambda r: (not r[2].up, r[1]))

    if not rows:
        body = [f"{fg(C_DIMTX)}no inference on this fleet{RESET}"]
    else:
        body = []
        multi = len(hosts) > 1
        for h, port, p in rows:
            col = C_OK if p.up else C_BAD
            # The host column earns its width only when there is more than one
            # host to tell apart; otherwise it is padding on every row.
            lead = f"{fg(C_CYAN)}{h.display[:10]:<10} " if multi else ""
            lat = f" {p.latency * 1000:.0f}ms" if p.up and p.latency else ""
            body.append(clip(
                f"{dot(col, '●' if p.up else '○')} {lead}"
                f"{fg(C_FRAME)}:{port:<5}{RESET}"
                f"{fg(C_TEXT)}{ell(p.label or '?', 13):<13}"
                f"{fg(col)}{ell(p.detail or p.note, 13):<13}"
                f"{fg(C_DIMTX)}{lat}{RESET}", w))
    up = sum(1 for _, _, p in rows if p.up)
    right = f"{up}/{len(rows)} up" if rows else ""
    return fill("LLM SERVERS", body, region, right)


# --- AGENTS ------------------------------------------------------------------

#: One blank cell between columns. Headers and values must not touch, or
#: "PIDKIND" reads as a single word and the whole table looks misaligned.
GAP = 2

# (key, label, width, align) in the order they should be dropped.
AGENT_COLS = [
    ("host", "HOST", 10, "<"),
    ("pid", "PID", 7, ">"),
    ("kind", "KIND", 8, "<"),
    ("model", "MODEL", 10, "<"),
    ("project", "PROJECT", 15, "<"),
    ("work", "WORKING ON", 26, "<"),
    ("cpu", "CPU", 5, ">"),
    ("rss", "RSS", 7, ">"),
    ("age", "AGE", 8, ">"),
]

DROP_ORDER = ["host", "rss", "age", "pid", "kind", "cpu", "project", "model",
              "work"]

COL_COLOR = {"host": C_CYAN, "pid": C_DIMTX, "kind": C_LIVE, "model": C_MAG,
             "project": C_LABEL, "work": C_TEXT, "cpu": C_TEXT,
             "rss": C_DIMTX, "age": C_DIMTX}


def fit_cols(w: int) -> list[tuple]:
    """The widest column set that fits ``w``.

    Columns are dropped one at a time, least valuable first, rather than every
    column being squeezed. A truncated WORKING ON column is unreadable; losing
    the host column costs nothing on a single-host view.
    """
    cols = list(AGENT_COLS)
    while cols:
        if sum(c[2] + GAP for c in cols) <= w:
            return cols
        for drop in DROP_ORDER:
            for i, c in enumerate(cols):
                if c[0] == drop:
                    cols.pop(i)
                    break
            else:
                continue
            break
        else:
            return []
    return cols


def all_agents(hosts: list[Host]) -> list[tuple[Host, Agent]]:
    rows = [(h, a) for h in hosts for a in h.agents]
    rows.sort(key=lambda ha: (ha[1].cpu, ha[1].age), reverse=True)
    return rows


def p_agents(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    """The agent table -- the panel the whole thing exists for."""
    w = region.inner
    rows = all_agents(hosts)

    if not rows:
        return fill("AGENTS", [f"{fg(C_DIMTX)}no agents running{RESET}"],
                    region, right="0")

    cols = fit_cols(w)
    if not cols:
        return fill("AGENTS", [f"{fg(C_DIMTX)}too narrow for the table{RESET}"],
                    region)

    # Every cell is fitted to its column width and the cells are then joined by
    # a real gap. Widening the format spec instead lets a right-aligned number
    # sit flush against the next header -- "PIDKIND" -- which reads as one word
    # and makes the whole table look misaligned.
    def cell(v: str, cw: int, al: str) -> str:
        return pad(ell(v, cw), cw, ">" if al == ">" else "<")

    head = (" " * GAP).join(
        cell(lab, cw, al) for _, lab, cw, al in cols)
    body = [f"{fg(C_DIMTX)}{head}{RESET}"]

    limit = max(1, region.rows - 1)
    for h, a in rows[:limit]:
        vals = {
            "host": h.display,
            "pid": str(a.pid),
            "kind": a.kind,
            "model": a.model or "-",
            "project": a.project or "-",
            "work": a.work or a.state or "-",
            "cpu": f"{a.cpu:.1f}",
            "rss": f"{a.rss}M" if a.rss else "-",
            "age": human_age(a.age),
        }
        line = ""
        for key, _, cw, al in cols:
            v = vals.get(key, "")
            c = COL_COLOR.get(key, C_TEXT)
            if key == "cpu" and v != "-":
                c = ramp_at(min(1.0, float(v) / 100.0))
            elif key == "age" and a.bound and a.cpu > 0:
                c = DIM
            line += f"{fg(c)}{cell(v, cw, al)}{RESET}" + " " * GAP
        body.append(clip(line, w))

    more = len(rows) - limit
    if more > 0:
        body.append(f"{fg(C_FRAME)}  +{more} more{RESET}")
    return fill("AGENTS", body, region, right=str(len(rows)))


def _agents_natural(hosts: list[Host]) -> int:
    # +1 header row, +2 border rows.
    return len(all_agents(hosts)) + 3


# --- SESSIONS ----------------------------------------------------------------

def p_sessions(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    w = region.inner
    rows: list[Session] = []
    for h in hosts:
        rows.extend(h.sessions)
    rows.sort(key=lambda s: s.age)

    if not rows:
        body = [f"{fg(C_DIMTX)}no transcripts{RESET}"]
    else:
        body = []
        for s in rows:
            age_c = C_LIVE if s.age < 120 else C_DIMTX
            body.append(clip(
                f"{fg(C_MAG)}{ell(s.model or '-', 11):<11}"
                f"{fg(C_LABEL)}{ell(s.project, 14):<14}"
                f"{fg(C_TEXT)}{ell(s.last_tool or s.prompt or 'idle', 18):<18}"
                f"{fg(age_c)}{human_age(s.age):>7}{RESET}", w))
    return fill("SESSIONS", body, region, right=str(len(rows)) if rows else "")


def _sessions_natural(hosts: list[Host]) -> int:
    return sum(len(h.sessions) for h in hosts) + 2


# --- READING -----------------------------------------------------------------

def p_reading(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    """Trend lines for the metrics that flap.

    Every number on the dashboard is a snapshot, so nothing about the frame
    itself distinguishes "live and boring" from "wedged and boring". A short
    history makes flapping visible without a second tool.
    """
    w = region.inner
    body = []
    get = getattr(hist, "get", None)
    for h in hosts:
        cpu_s = get(("cpu", h.name), []) if get else []
        mem_s = get(("mem", h.name), []) if get else []
        net_s = get(("net", h.name), []) if get else []
        if not cpu_s:
            continue
        delta = (net_s[-1] - net_s[0]) if len(net_s) > 1 else 0
        sw = max(6, min(20, (w - 34) // 2))
        body.append(clip(
            f"{fg(C_CYAN)}{ell(h.display, 10):<10}"
            f"{fg(C_DIMTX)}cpu {RESET}{spark(cpu_s, sw)} "
            f"{fg(C_DIMTX)}mem {RESET}{spark(mem_s, sw)} "
            f"{fg(C_DIMTX)}{human_n(delta)}/s{RESET}", w))
    if not body:
        body = [f"{fg(C_DIMTX)}collecting history...{RESET}"]
    return fill("READING", body, region)


# --- TMUX --------------------------------------------------------------------

def p_tmux(hosts: list[Host], region: Region, cfg, hist) -> list[str]:
    """The tmux inventory, and the command that gets you to it.

    Surfacing the attach command is the difference between a dashboard you
    read and one you act on: the panel says what is busy, this says how to
    reach it.
    """
    w = region.inner
    panes = []
    for h in hosts:
        panes.extend((h.display, p) for p in h.tmux)
    if not panes:
        body = [f"{fg(C_DIMTX)}no tmux sessions seen{RESET}"]
    else:
        body = []
        for host, p in panes:
            col = C_BAD if p.dead else (C_LIVE if p.agent else C_DIMTX)
            body.append(clip(
                f"{fg(C_DIMTX)}{ell(host, 9):<9}"
                f"{fg(C_PURP)}{ell(p.session, 12):<12}"
                f"{fg(C_FRAME)}{p.window:<7}"
                f"{fg(col)}{ell(p.cmd, 12):<12}"
                f"{fg(C_DIMTX)}{p.pid:>7}{RESET}", w))
    hint = ""
    for _, p in panes:
        if p.session and p.cmd not in ("bash", "zsh", "sh", "fish"):
            hint = f"tmux attach -t {p.session}"
            break
    if hint:
        body.append(clip(f"  {fg(C_GOLD)}{hint}{RESET}", w))
    return fill("TMUX", body, region, right=str(len(panes)) if panes else "")


def _tmux_natural(hosts: list[Host]) -> int:
    return sum(len(h.tmux) for h in hosts) + 3   # panes + attach hint + borders


# --- registry ----------------------------------------------------------------
#
# A panel declares its own minimum size and where it prefers to live. The
# solver reads these; nothing here knows the terminal size, which is what lets
# a panel be tested by handing it a 40x8 region and nothing else.

@dataclass
class Panel:
    """A panel function bound to the space it needs."""
    fn: object
    node: Node


def _natural(fn):
    """Wrap a height estimator so the solver can call it safely."""
    def probe(hosts, cfg):
        return fn(hosts, cfg)
    return probe


def _panel(fn, name, min_w, min_h, weight, side, natural=None, want_h=0,
           fill_space=False):
    node = Node(name=name, min_w=min_w, min_h=min_h, want_h=want_h,
                natural=natural, fill=fill_space, weight=weight, side=side)
    return Panel(fn, node)


PANELS = {
    "agents": _panel(p_agents, "agents", 44, 6, 10.0, "main",
                     natural=lambda hs, c: _agents_natural(hs), fill_space=True),
    "attention": _panel(p_attention, "attention", 28, 4, 8.0, "rail",
                        natural=lambda hs, c: len(collect_attention(hs, c)) + 3),
    "system": _panel(p_system, "system", 34, 6, 9.0, "rail",
                     natural=lambda hs, c: _system_natural(hs, c)),
    "llm": _panel(p_llm, "llm", 30, 4, 7.0, "rail",
                  natural=lambda hs, c: sum(len(h.ports) for h in hs) + 3),
    "tmux": _panel(p_tmux, "tmux", 30, 4, 5.0, "rail",
                   natural=lambda hs, c: _tmux_natural(hs), fill_space=True),
    "reading": _panel(p_reading, "reading", 36, 4, 3.0, "main",
                      natural=lambda hs, c: len(hs) + 3),
    "sessions": _panel(p_sessions, "sessions", 36, 4, 4.0, "main",
                       natural=lambda hs, c: _sessions_natural(hs),
                       fill_space=True),
}

#: Narrow-mode priority. Attention is first because in a small pane the one
#: thing that must survive every resize is "does anything need me" -- the agent
#: table, useful as it is, is a longer scroll. Wide mode ignores this order and
#: routes by ``Node.side``.
ORDER = ["attention", "agents", "system", "llm", "tmux", "reading", "sessions"]


def enabled(cfg) -> list[str]:
    if not cfg.panels:
        return [p for p in ORDER if p not in (cfg.hidden_panels or [])]
    return [p for p in ORDER if p in cfg.panels]


def nodes_for(cfg, hosts) -> list[Node]:
    """Copy the registry's nodes with this frame's natural heights bound in.

    The registry entries are shared singletons, so they are copied rather than
    mutated: a frame built on one machine's data must not leave its height
    hints lying around for the next one.
    """
    import dataclasses

    out = []
    for name in enabled(cfg):
        panel = PANELS.get(name)
        if panel is None:
            continue
        est = panel.node.natural
        if est is None:
            out.append(panel.node)
            continue
        try:
            probe = lambda e=est: e(hosts, cfg)          # noqa: E731
        except Exception:
            probe = None
        out.append(dataclasses.replace(panel.node, natural=probe))
    return out
