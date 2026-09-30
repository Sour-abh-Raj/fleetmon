"""Command line.

Five modes, because a dashboard has to be both a viewer and a data source:

``fleetmon``                    the live TUI
``fleetmon --collect --raw``    one JSON snapshot, for a peer to pull
``fleetmon --once``             one frame, printed, no alt screen -- for
                                piping, tmux capture-pane and CI
``fleetmon --serve [port]``     HTTP, for peers behind NAT
``fleetmon --write-inbox DIR``  drop a snapshot into a shared directory

The separation of ``--collect`` from the TUI is what makes the fleet
two-way-able without either machine ever opening an inbound port by default.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import EXAMPLE, parse_args, save_example

VERSION = "0.1.0"

USAGE = f"""\
fleetmon {VERSION} -- live view of the agents and inference servers on a fleet

USAGE
  fleetmon [options]                  live dashboard
  fleetmon --once                     one frame, printed and exit
  fleetmon --collect --raw            JSON snapshot for a peer to pull
  fleetmon --serve [PORT]             serve snapshots over HTTP (default 8766)
  fleetmon --write-inbox DIR          write a snapshot into DIR
  fleetmon --init-config [PATH]       write an example config
  fleetmon --check                    report detected collectors and peers

VIEWING
  --peer HOST[:PORT]      fetch a snapshot (repeatable). ssh / http / a path
                          are all tried, first to answer wins.
  --once                  render one frame and exit
  --raw                   emit JSON instead of the TUI
  --no-alt                do not use the alternate screen, keeping the
                          terminal's own scrollback as well

PEERING
  --label NAME            how this machine is named in other machines' views
  --collect               take a snapshot and print it
  --serve [PORT]          HTTP server for peers to poll
  --write-inbox DIR       write snapshots to DIR/<host>.json
  --interval SECONDS      how often peers are polled

DISPLAY
  --refresh SECONDS       frame interval (default 1.0)
  --history N             frames kept for scrollback (default 400)
  --wide-at COLS          collapse to one column below this width (default 132)
  --panels A,B,C          only these panels
  --hide-panels A,B       all but these panels
  --theme NAME            colour theme
  --mouse                 enable mouse reporting
  --no-gpu                skip GPU probes
  --no-probe              skip HTTP port probes
  --host NAME             override the detected hostname

CONFIG
  Searched in order, later wins:
    ./fleetmon.toml
    ~/.config/fleetmon/fleetmon.toml
    /etc/fleetmon/fleetmon.toml
  Override with --config PATH or $FLEETMON_CONFIG.
  Every flag above also has an FLEETMON_* environment variable.

KEYS (in the live view)
  q quit   j/k scroll   PgUp/PgDn page   G back to live   p pause
  r reset  w narrow/wide toggle         ? help
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--init-config" in argv:
        i = argv.index("--init-config")
        path = Path(argv[i + 1]) if i + 1 < len(argv) and not argv[i + 1].startswith("-") \
            else Path.home() / ".config" / "fleetmon" / "fleetmon.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        save_example(path)
        print(f"wrote {path}")
        return 0

    cfg, extra, special = parse_args(argv)

    if special == "help":
        print(USAGE)
        return 0
    if special == "version":
        from . import __version__
        print(f"fleetmon {__version__}")
        return 0

    from .console import strip_env
    strip_env()

    if "--check" in argv:
        return _check(cfg)
    if "--serve" in argv:
        return _serve(cfg, argv)
    if "--collect" in argv:
        return _collect(cfg)
    if "--write-inbox" in argv:
        return _write_inbox(cfg, argv)

    from .app import run
    return run(cfg)


def _collect(cfg) -> int:
    from .collectors import collect
    snap = collect(cfg)
    sys.stdout.write(snap.__json__() + "\n")
    return 0


def _write_inbox(cfg, argv) -> int:
    i = argv.index("--write-inbox")
    target = Path(argv[i + 1]) if i + 1 < len(argv) and not argv[i + 1].startswith("-") \
        else Path.home() / ".fleetmon" / "inbox"
    from .collectors import collect
    from .model import Snapshot
    snap = collect(cfg)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{cfg.display}.json"
    # Write-then-rename so a reader never observes a half-written file.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(snap.__json__(), encoding="utf-8")
    tmp.replace(path)
    print(str(path))
    return 0


def _serve(cfg, argv) -> int:
    i = argv.index("--serve")
    port = 8766
    if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
        try:
            port = int(argv[i + 1])
        except ValueError:
            pass
    from .server import serve
    return serve(cfg, port)


def _check(cfg) -> int:
    """Report what this machine can actually see, before it is trusted."""
    from .collectors import collect, names
    from .collectors.ports import listening
    from .collectors.tmux import available as tmux_available
    from .collectors.system import psutil as _ps

    out = []
    out.append(f"fleetmon {VERSION}")
    out.append(f"  host        {cfg.display}")
    out.append(f"  python      {sys.version.split()[0]}  ({sys.platform})")
    out.append(f"  psutil      {'yes' if _ps else 'no -- using fallbacks'}")
    out.append(f"  tomllib     {'yes' if _has_tomllib() else 'no -- JSON config only'}")
    out.append(f"  tmux        {'yes' if tmux_available() else 'no'}")
    out.append(f"  alt screen  {'yes' if cfg.alt_screen else 'no'}")
    out.append(f"  config      {', '.join(cfg.sources) or '(defaults)'}")
    out.append(f"  collectors  {', '.join(names())}")
    ports = sorted(listening())
    out.append(f"  listening   {', '.join(str(p) for p in ports) or '(none)'}")
    out.append(f"  peers       {len(cfg.hosts)} configured")
    for h in cfg.hosts:
        out.append(f"    - {h.name}: {h.via} {h.target}")
    snap = collect(cfg)
    h = snap.host
    out.append(f"  collected   {len(h.agents)} agents, {len(h.sessions)} sessions, "
               f"{len(h.ports)} servers, {len(h.gpus)} gpus")
    if h.alerts:
        for a in h.alerts:
            out.append(f"  ! {a}")
    print("\n".join(out))
    return 0


def _has_tomllib() -> bool:
    try:
        import tomllib      # noqa: F401
        return True
    except ImportError:
        return False
