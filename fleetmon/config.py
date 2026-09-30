"""Configuration: TOML in, a frozen :class:`Config` out.

Precedence, lowest to highest:

1. the defaults in this file
2. ``./fleetmon.toml`` then ``~/.config/fleetmon/fleetmon.toml``
   (later files win, so a user file overrides a checked-in one)
3. ``$FLEETMON_CONFIG``
4. ``FLEETMON_*`` environment variables
5. command-line flags

The layered design is not decoration. A dashboard that only reads a file in
the current directory is a toy; one that reads a global file is what you
actually want when the same config drives a laptop, a workstation and a
headless server.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

APP = "fleetmon"

SEARCH = (
    Path.cwd() / "fleetmon.toml",
    Path.cwd() / ".fleetmon.toml",
    Path.home() / ".config" / APP / "fleetmon.toml",
    Path.home() / ".fleetmon.toml",
    Path("/etc") / APP / "fleetmon.toml",
)

ENV_MAP = {
    "label": "FLEETMON_LABEL",
    "refresh": "FLEETMON_REFRESH",
    "theme": "FLEETMON_THEME",
    "history": "FLEETMON_HISTORY",
    "host": "FLEETMON_HOSTNAME",
    "no_alt": "FLEETMON_NO_ALT",
    "mouse": "FLEETMON_MOUSE",
    "config": "FLEETMON_CONFIG",
}


def load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file, or a JSON file wearing a .toml extension.

    ``tomllib`` only exists on 3.11+. Rather than take a dependency for a
    config parser -- the whole package is stdlib-only by design -- an older
    interpreter is told exactly what to do instead of failing obscurely.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json" or text.lstrip().startswith("{"):
        return json.loads(text)
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - version dependent
        raise SystemExit(
            f"{path}: TOML needs Python 3.11+ (tomllib). Either upgrade, or "
            f"convert the file to JSON and name it {path.stem}.json"
        ) from exc
    return tomllib.loads(text)


@dataclass
class HostRef:
    """A machine to pull from."""
    name: str
    via: str = "file"              # file | ssh | http
    target: str = ""               # path, host, or URL
    label: str = ""
    interval: float = 0.0          # 0 = default
    timeout: float = 12.0
    color: int | None = None


@dataclass
class Config:
    # -- identity
    label: str = ""                # shown instead of hostname
    hostname: str = ""             # override detected hostname

    # -- behaviour
    refresh: float = 1.0
    history: int = 400             # frames retained for the pager
    alt_screen: bool = True
    mouse: bool = False
    once: bool = False             # render one frame and exit
    raw: bool = False              # emit JSON instead of a TUI

    # -- presentation
    theme: str = "default"
    panels: list[str] = field(default_factory=list)       # empty = all enabled
    hidden_panels: list[str] = field(default_factory=list)
    min_width: int = 60
    min_height: int = 18
    wide_at: int = 132              # below this, collapse to one column
    show_cursor_hint: bool = True
    fps: int = 0                   # 0 = as fast as the collectors allow

    # -- collection
    collect: bool = True
    probe_ports: bool = True
    probe_timeout: float = 1.2
    read_transcripts: bool = True
    transcript_budget: int = 1 << 21
    max_transcripts: int = 48
    gpu: bool = True

    # -- fleet
    hosts: list[HostRef] = field(default_factory=list)
    peer_path: str = ""            # where to drop our own snapshot
    push: list[str] = field(default_factory=list)

    # -- thresholds
    warn_cpu: float = 78.0
    crit_cpu: float = 92.0
    warn_mem: float = 85.0
    crit_mem: float = 94.0
    warn_disk: float = 88.0
    crit_disk: float = 95.0
    stale_host: float = 8.0        # seconds before a peer is called stale

    # -- agent detection
    agent_patterns: list[list[str]] = field(default_factory=list)
    ignore_cmd: list[str] = field(default_factory=list)

    # -- provenance
    sources: list[str] = field(default_factory=list)
    path: Path | None = None

    # -- derived ------------------------------------------------------------

    @property
    def display(self) -> str:
        return self.label or self.hostname or _detect_hostname()

    def enabled(self, name: str) -> bool:
        if name in self.hidden_panels:
            return False
        return not self.panels or name in self.panels


def _detect_hostname() -> str:
    import socket
    try:
        return socket.gethostname().split(".")[0] or "host"
    except Exception:
        return "host"


def discover() -> Config:
    """Build a config from the search path plus the environment."""
    cfg = Config()
    data: dict[str, Any] = {}

    for path in SEARCH:
        try:
            if path.is_file():
                data = _deep_merge(data, load_toml(path))
                cfg.sources.append(str(path))
        except OSError:
            continue

    explicit = os.environ.get("FLEETMON_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            data = _deep_merge(data, load_toml(p))
            cfg.sources.append(str(p))

    cfg = apply(cfg, data)

    env = {}
    for key, var in ENV_MAP.items():
        val = os.environ.get(var)
        if val is None:
            continue
        if key == "config":
            continue
        if key == "refresh":
            env[key] = float(val)
        elif key in ("history", "min_width", "min_height", "wide_at", "fps",
                     "max_transcripts"):
            env[key] = int(val)
        elif key in ("no_alt", "mouse"):
            env[key] = val.lower() not in ("0", "false", "no", "")
        else:
            env[key] = val
    if env:
        cfg = apply(cfg, env)

    if not cfg.hostname:
        cfg.hostname = _detect_hostname()
    return cfg


def apply(cfg: Config, data: dict[str, Any]) -> Config:
    """Fold a mapping onto a config, validating as we go."""
    if not data:
        return cfg

    fleet = data.pop("fleet", None)
    detect = data.pop("detect", None)
    limits = data.pop("limits", None)
    ui = data.pop("ui", None)
    known = {f for f in cfg.__dataclass_fields__}

    for section in (fleet, detect, limits, ui):
        if isinstance(section, dict):
            data = {**data, **section}

    hosts = data.pop("hosts", None)
    if hosts:
        refs = []
        for h in hosts:
            if isinstance(h, str):
                refs.append(HostRef(name=h, target=h, via="ssh"))
            elif isinstance(h, dict):
                name = h.get("name") or h.get("host") or "peer"
                refs.append(HostRef(
                    name=name,
                    via=h.get("via", "ssh" if name != name.split(".")[0] else "ssh"),
                    target=h.get("target") or h.get("host") or name,
                    label=h.get("label", ""),
                    interval=float(h.get("interval", 0) or 0),
                    timeout=float(h.get("timeout", 12) or 12),
                    color=h.get("color"),
                ))
        cfg.hosts = refs

    push = data.pop("push", None)
    if push:
        refs = []
        for p in (push if isinstance(push, list) else [push]):
            if isinstance(p, str):
                # A bare path is the common case: "share this snapshot there".
                refs.append(HostRef(name=Path(p).name or "peer", via="file",
                                    target=p))
            elif isinstance(p, dict):
                name = p.get("name") or Path(p.get("target", "peer")).name
                refs.append(HostRef(name=name,
                                    via=p.get("via", "file"),
                                    target=p.get("target", ""),
                                    label=p.get("label", ""),
                                    timeout=float(p.get("timeout", 12) or 12)))
        cfg.push = refs

    unknown = set(data) - known
    for k in unknown:
        data.pop(k)

    clean: dict[str, Any] = {}
    for k, v in data.items():
        cur = getattr(cfg, k, None)
        try:
            if isinstance(cur, bool):
                clean[k] = bool(v)
            elif isinstance(cur, float):
                clean[k] = float(v)
            elif isinstance(cur, int):
                clean[k] = int(v)
            elif isinstance(cur, list) and not isinstance(v, list):
                clean[k] = [v]
            else:
                clean[k] = v
        except (TypeError, ValueError):
            continue
    return replace(cfg, **clean) if clean else cfg


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_args(argv: list[str] | None = None) -> tuple[Config, list[str], str]:
    """Lightweight argument parser.

    Deliberately not argparse: the dashboard is a live tool people launch from
    a tmux binding, and a subcommand-driven help screen is more friction than
    ``--help`` output is worth here. Unknown flags fall through to ``extra``
    so plugins can claim their own.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    data: dict[str, Any] = {}
    extra: list[str] = []
    positional: list[str] = []
    explicit = ""

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            return Config(), extra, "help"
        if a in ("-V", "--version"):
            return Config(), extra, "version"
        if not a.startswith("-"):
            positional.append(a)
            i += 1
            continue
        name = a.lstrip("-").replace("-", "_")
        val: Any = None
        if "=" in name:
            name, val = name.split("=", 1)
        elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            val = argv[i + 1]
            i += 1
        if val is None:
            val = True
        if name == "peer":
            data.setdefault("hosts", []).append({"name": val, "via": "auto"})
        elif name == "config":
            explicit = str(val)
        elif name == "collect":
            data["collect"] = True
        elif name in ("once", "raw", "no_alt", "mouse", "no_gpu", "no_probe"):
            data[name if name != "no_gpu" else "gpu"] = (
                False if name.startswith("no_") else True)
        elif name in ("refresh", "history", "min_width", "min_height", "wide_at",
                      "fps", "max_transcripts"):
            data[name] = val
        else:
            extra.extend([a] if val is True else [a, str(val)])
        i += 1

    if positional:
        data.setdefault("hosts", []).extend(
            [{"name": p, "via": "ssh", "target": p} for p in positional])

    cfg = discover()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            cfg = apply(cfg, load_toml(p))
            cfg.sources.append(str(p))
    cfg = apply(cfg, data)
    return cfg, extra, ""


def save_example(path: Path) -> None:
    path.write_text(EXAMPLE, encoding="utf-8")


EXAMPLE = """\
# fleetmon -- example configuration
#
# Every key below is optional; the defaults are shown. Drop this at
# ~/.config/fleetmon/fleetmon.toml (all machines) or ./fleetmon.toml
# (just this checkout). A local file wins over a global one.

[ui]
label = ""                  # shown instead of the hostname
refresh = 1.0               # seconds between frames
history = 400               # frames kept for scrollback
alt_screen = true           # false keeps real terminal scrollback too
mouse = false
wide_at = 132               # collapse to one column below this width
min_width = 60
min_height = 18
# panels = ["system", "agents", "sessions", "llm", "reading"]
# hidden_panels = ["sessions"]

[collect]
probe_ports = true          # HTTP health probes on configured ports
probe_timeout = 1.2
read_transcripts = true     # parse ~/.claude/projects/*.jsonl
max_transcripts = 48
transcript_budget = 2097152
gpu = true

# Machines to pull from. Each returns the same snapshot this one produces.
[[hosts]]
name = "nixos"              # label in the UI
via = "ssh"                 # ssh | file | http
target = "tunix"            # ssh host, path, or http://host:port/
interval = 2.0              # 0 = same as refresh
# color = 141

# [[hosts]]
# name = "shared"
# via = "file"
# target = "~/fleetmon-inbox"

[limits]
warn_cpu = 78.0
crit_cpu = 92.0
warn_mem = 85.0
crit_mem = 94.0
warn_disk = 88.0
crit_disk = 95.0
stale_host = 8.0            # seconds before a silent peer is called stale

[detect]
# Extra agent signatures: label, then a regex matched against the command line.
# agent_patterns = [["paper-run", "regen_paper\\\\.py"], ["scraper", "scrapy"]]
# Never show these, matched as substrings.
# ignore_cmd = ["grep", "vim"]
"""
