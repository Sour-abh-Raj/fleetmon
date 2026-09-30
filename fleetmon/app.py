"""The live view: collection, aggregation, layout, input.

The render loop is deliberately boring. Every frame:

1. ask the terminal for its size -- no cached geometry, no resize handler
2. solve the layout for that size
3. build each panel into its region
4. push the frame into the pager's history
5. draw the frame the pager says is current

Step 1 is the whole answer to "it should adjust to the terminal". Caching
geometry and listening for resize events is the fast approach; it is wrong on
Windows (where the event is ``SetConsoleCtrlHandler``, not ``SIGWINCH``), wrong
inside tmux split panes (where the size can change without any signal to the
child), and wrong after a suspend. Asking costs microseconds against a frame
budget of tens of milliseconds.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import panels as panels_mod
from .collectors import collect
from .config import Config, HostRef
from .console import (
    BOLD, C_BAD, C_DIMTX, C_FRAME, C_GOLD, C_INFO, C_LABEL, C_LIVE, C_OK,
    C_TEXT, C_WARN, DIM, RESET, box, clip, enable_vt, fg, pad, set_font,
    strip, strip_env, wlen,
)
from .layout import Solver, render
from .model import Host, Snapshot, human_age, human_n
from .term import Pager, Terminal


class Fleet:
    """Owns the host table and the background refresh."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hosts: dict[str, Host] = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.errors: dict[str, str] = {}

    def start(self) -> None:
        if self.cfg.collect:
            threading.Thread(target=self._local_loop, name="fm-local",
                             daemon=True).start()
        for h in self.cfg.hosts:
            threading.Thread(target=self._peer_loop, args=(h,),
                             name=f"fm-peer-{h.name}", daemon=True).start()

    def _local_loop(self) -> None:
        while not self.stop.is_set():
            t0 = time.time()
            try:
                snap = collect(self.cfg)
                self._put(snap.host)
                self.errors.pop("local", None)
                if self.cfg.peer_path or self.cfg.push:
                    self._publish(snap)
            except Exception as exc:
                self.errors["local"] = f"{type(exc).__name__}: {exc}"
            self.stop.wait(max(0.2, self.cfg.refresh - (time.time() - t0)))

    def _publish(self, snap) -> None:
        """Make this machine's own snapshot available to whoever is watching.

        Two-way visibility is not a viewer feature, it is a publisher feature:
        the machine that *has* the data is the one that has to hand it over.
        Without this, a headless box can be watched but never watched back.
        """
        from .transport import FileTransport, build

        if self.cfg.peer_path:
            ref = HostRef(name=self.cfg.display, via="file",
                          target=str(Path(self.cfg.peer_path).expanduser()
                                     / f"{self.cfg.display}.json"))
            if not FileTransport().push(ref, snap):
                self.errors["publish"] = "peer_path write failed"
            else:
                self.errors.pop("publish", None)
        for ref in self.cfg.push:
            try:
                tr = build(ref.via, [ref])[ref.name]
                ok = tr.push(ref, snap)
            except Exception as exc:
                self.errors[f"push:{ref.name}"] = f"{type(exc).__name__}"
                continue
            if ok:
                self.errors.pop(f"push:{ref.name}", None)
            else:
                self.errors[f"push:{ref.name}"] = "push failed"

    def _peer_loop(self, ref) -> None:
        from .transport import build
        tr = build(ref.via, [ref])[ref.name]
        every = ref.interval or max(self.cfg.refresh * 2, 2.0)
        while not self.stop.is_set():
            t0 = time.time()
            self._poll(ref, tr)
            self.stop.wait(max(0.3, every - (time.time() - t0)))

    def _poll(self, ref, tr) -> None:
        """One fetch of one peer.

        Split out from the loop so a caller can drive a single poll
        synchronously. The loop itself is unbounded by design and has no other
        way in, which made "fetch this peer exactly once" untestable.
        """
        try:
            snap = tr.fetch(ref)
            if snap is None:
                self.errors[ref.name] = "unreachable"
                return
            # The config names the peer, so the config wins: a machine that
            # renamed itself should still appear under the name you asked for.
            snap.host.name = ref.name
            snap.host.label = ref.label or snap.host.label or ref.name
            snap.host.self_host = False
            self._put(snap.host)
            self.errors.pop(ref.name, None)
        except Exception as exc:
            self.errors[ref.name] = f"{type(exc).__name__}: {exc}"

    def _put(self, host: Host) -> None:
        with self.lock:
            self.hosts[host.name] = host

    def view(self) -> list[Host]:
        with self.lock:
            hosts = list(self.hosts.values())
        return sorted(hosts, key=lambda h: (not h.self_host, h.display))

    def shutdown(self) -> None:
        self.stop.set()


class History:
    """Ring buffers for the trend panel."""

    def __init__(self, depth: int = 240):
        self.depth = depth
        self.data: dict[tuple, deque] = {}

    def record(self, hosts: list[Host]) -> None:
        for h in hosts:
            self._push(("cpu", h.name), h.cpu.pct)
            self._push(("mem", h.name), h.mem.pct)
            self._push(("net", h.name), h.net.rx)
            for i, g in enumerate(h.gpus[:1]):
                self._push((f"gpu{i}", h.name), g.pct)

    def _push(self, key, value) -> None:
        d = self.data.get(key)
        if d is None:
            d = self.data[key] = deque(maxlen=self.depth)
        d.append(float(value))

    def get(self, key, default=None):
        return list(self.data.get(key, default or []))


class App:
    """The interactive dashboard."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fleet = Fleet(cfg)
        self.history = History()
        self.pager = Pager(history=deque(maxlen=max(16, cfg.history)))
        self.term = Terminal(alt_screen=cfg.alt_screen, mouse=cfg.mouse)
        self.paused = False
        self.force_narrow = False
        self.panel_offset = 0
        self.last_frame = ""
        self.frame_no = 0
        self.started = time.time()

    # -- header / footer -----------------------------------------------------

    def header(self, hosts: list[Host], w: int, h: int, live: bool) -> list[str]:
        now = time.strftime("%H:%M:%S")
        title = (f"{fg(C_GOLD)}{BOLD} fleetmon {RESET}"
                 f"{fg(C_DIMTX)}{self.cfg.display}{RESET}")
        if len(hosts) > 1:
            title += f"{fg(C_DIMTX)} +{len(hosts) - 1} peer" \
                     f"{'s' if len(hosts) != 2 else ''}{RESET}"
        age = time.time() - self.started
        right = (f"{fg(C_DIMTX)}{now}  {w}x{h}  "
                 f"{fg(C_LIVE) if live else fg(C_WARN)}"
                 f"{'LIVE' if live else f'-{self.pager.pos}'}{RESET}  "
                 f"{fg(C_DIMTX)}up {human_age(age)}{RESET}")
        gap = max(1, w - wlen(title) - wlen(right))
        return [clip(title + " " * gap + right, w), ""]

    def footer(self, hosts: list[Host], w: int, extra: list[str]) -> list[str]:
        keys = [("q", "quit"), ("j/k", "scroll"), ("pgup/dn", "page"),
                ("G", "live"), ("tab", "panels"), ("p", "pause"),
                ("w", "width"), ("?", "help")]
        # Drop whole hints rather than letting ``clip`` cut one in half: "pgup/dn
        # pa" is a rendering bug, whereas a shorter list is a deliberate choice
        # the reader can still act on. Errors come first and are never dropped,
        # because they are the reason the reader is looking at the footer --
        # but a list too long for the row is abbreviated to fit, since a
        # half-printed list of hidden panels names fewer of them than it claims.
        if extra:
            joined = "  ".join(extra)
            if wlen(joined) > max(0, w - 8):
                names = [e.split(":", 1)[-1].split()[-1] for e in extra]
                joined = f"hidden: {', '.join(names[:3])}"
                if len(names) > 3:
                    joined += f" +{len(names) - 3}"
            prefix = f"{fg(C_WARN)}{joined}{RESET}  "
        else:
            prefix = ""
        room = max(0, w - wlen(strip(prefix)))
        while keys and wlen(strip(prefix + self._keyline(keys))) > room:
            keys.pop()
        return [clip(prefix + self._keyline(keys), w)]

    @staticmethod
    def _keyline(keys: list[tuple[str, str]]) -> str:
        return f"{fg(C_FRAME)} · {RESET}".join(
            f"{fg(C_LABEL)}{a}{RESET}{fg(C_DIMTX)} {b}{RESET}" for a, b in keys)

    # -- frame ---------------------------------------------------------------

    def build_frame(self, size: tuple[int, int] | None = None) -> str:
        """One frame as a string.

        ``size`` overrides the terminal, and with it the ``min_width`` clamp --
        the clamp exists to stop the *live* view from collapsing when someone
        drags a pane to nothing, which is not a concern when the caller has
        already said how big the output is. Honouring the request exactly is
        what makes this usable for testing a layout at a hostile geometry.
        """
        hosts = self.fleet.view()
        if size is not None:
            w, h = max(20, int(size[0])), max(8, int(size[1]))
        else:
            w, h = self.term.size()
            w = max(self.cfg.min_width, w)
            h = max(self.cfg.min_height, h)

        if not self.paused and not self.pager.live:
            # History keeps advancing so that pressing G after a minute of
            # reading gives you a minute of frames, not a frozen past.
            pass
        if not self.paused:
            self.history.record(hosts)

        names = panels_mod.enabled(self.cfg)
        nodes = panels_mod.nodes_for(self.cfg, hosts)
        if nodes and self.panel_offset:
            # Rotate the priority order. In a quarter-screen pane something
            # has to lose, and TAB making the loser come round is the
            # difference between "the window is too small" and "I cannot see
            # the sessions panel at all".
            k = self.panel_offset % len(nodes)
            nodes = nodes[k:] + nodes[:k]
        solver = Solver(w, h, self.cfg.wide_at)
        layout = solver.solve(nodes, header=2, footer=1,
                              force_narrow=self.force_narrow)

        builders = {n: (lambda region, n=n: panels_mod.PANELS[n].fn(
            hosts, region, self.cfg, self.history))
            for n in layout.order if n in panels_mod.PANELS}

        extra = []
        for name, msg in self.fleet.errors.items():
            extra.append(f"{name}: {msg}")
        dropped = [p.name for p in layout.dropped() if p.reason != "unplaced"]
        if dropped:
            extra.append(f"hidden at {w}x{h}: {', '.join(dropped)}")

        lines = render(layout, builders,
                       header=self.header(hosts, w, h, self.pager.live),
                       footer=self.footer(hosts, w, extra),
                       scrollbar=self.pager.scrollbar(h) if not self.pager.live else "")
        self.frame_no += 1
        return "\n".join(lines)

    # -- loop ----------------------------------------------------------------

    def run(self) -> int:
        self.fleet.start()
        if self.cfg.once:
            # Collect synchronously: spawning the refresh thread and printing
            # immediately would race it, and a one-shot render of an empty
            # dashboard is worse than no render.
            t0 = time.time()
            while not self.fleet.hosts and time.time() - t0 < 10:
                time.sleep(0.05)
            print(self.build_frame())
            self.fleet.shutdown()
            return 0
        with self.term:
            self.term.out.write("\x1b[2J")
            while not self.fleet.stop.is_set():
                frame = self.build_frame()
                if not self.paused:
                    self.pager.push(frame)
                shown = self.pager.view() or frame
                w, h = self.term.size()
                h = max(self.cfg.min_height, min(h, 200))
                self.term.draw(shown, h)

                if self._handle_keys():
                    break
                self.fleet.stop.wait(self.cfg.refresh if not self.paused else 0.25)
        self.fleet.shutdown()
        return 0

    def _handle_keys(self) -> bool:
        """Returns True when the app should exit."""
        page = max(1, self.term.size()[1] - 4)
        for k in self.term.poll():
            if self.pager.handle(k, page):
                continue
            n, c = k.name, k.char
            if n == "char" and c in ("q", "Q"):
                return True
            if n == "ctrl-c":
                return True
            if n == "char" and c in ("p", " "):
                self.paused = not self.paused
                if not self.paused:
                    self.pager.bottom()
            elif n == "char" and c in ("r", "R"):
                self.history = History()
                self.pager = Pager(history=deque(maxlen=self.pager.history.maxlen))
            elif n == "char" and c in ("w", "W"):
                self.force_narrow = not self.force_narrow
            elif n == "tab":
                self.panel_offset += 1
            elif n == "char" and c in ("?", "h", "H"):
                self.term.write_line(HELP)
            elif n == "char" and c in ("\x04", "\x15"):     # ctrl-d / ctrl-u
                self.pager.bottom()
        return False


HELP = """
fleetmon -- keys

  q          quit
  j / Down   scroll down one frame       k / Up    scroll up one frame
  PgDn       scroll down a page          PgUp      scroll up a page
  g / Home   jump to the oldest frame    G / End   jump back to live

  p          pause / resume the refresh
  r          reset trend history and frame buffer
  w          force narrow layout (the window will also do this on its own)
  Tab        rotate panel priority (which panel survives a small window)
  ?          this text

Scrollback is fleetmon's own, not the terminal's: every frame is kept in a
ring buffer so j/k work identically in tmux, in Windows Terminal, in a split
pane and over ssh. Press G to return to the live view.
"""


def run(cfg: Config) -> int:
    strip_env()
    if not cfg.once:
        enable_vt()
        set_font()
    return App(cfg).run()
