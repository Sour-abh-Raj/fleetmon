"""Collector registry.

A collector is any callable with the signature ``fn(ctx) -> Any``. Registering
one is a one-liner, which is what makes the dashboard extensible without a
plugin loader, an entry-point scan or a package to install:

    from fleetmon.collectors import register
    @register("my-thing")
    def my_thing(ctx):
        return subprocess.run(["my-thing", "--json"], ...).stdout
    # ...then `panels = ["my-thing"]` in the config.

Everything built in is registered the same way, so a user collector and a
built-in are indistinguishable downstream.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..model import Host, Snapshot
from . import ports as ports_mod
from . import processes as processes_mod
from . import sessions as sessions_mod
from . import system as system_mod
from . import tmux as tmux_mod

_REGISTRY: dict[str, Callable[[Any], Any]] = {}


def register(name: str):
    """Decorator: register a collector under ``name``."""

    def wrap(fn):
        _REGISTRY[name] = fn
        return fn

    return wrap


def get(name: str):
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY)


# --- built-ins ---------------------------------------------------------------

@register("system")
def _c_system(ctx):
    return system_mod.host_identity(ctx.cfg)


@register("agents")
def _c_agents(ctx):
    return ctx.procs.agents()


@register("sessions")
def _c_sessions(ctx):
    return ctx.tr.sessions()


@register("ports")
def _c_ports(ctx):
    return ctx.net.probe_all()


@register("tmux")
def _c_tmux(ctx):
    return tmux_mod.panes()


@register("pressure")
def _c_pressure(ctx):
    done, total = sessions_mod.todo_state()
    return {"done": done, "total": total,
            "projects": len(sessions_mod.projects_in_flight())}


# --- context -----------------------------------------------------------------

class CollectContext:
    """State shared between collectors within one snapshot."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.procs = processes_mod.Processes(cfg)
        self.tr = sessions_mod.Transcripts(cfg)
        self.net = ports_mod.Net(cfg)

    def tmux(self):
        return tmux_mod.panes()


def collect(cfg, only: list[str] | None = None) -> Snapshot:
    """Run the enabled collectors and fold their output into one snapshot.

    Each collector is isolated: one that raises costs its own panel, not the
    frame. A dashboard that dies because a ``nvidia-smi`` call failed is a
    dashboard nobody trusts.
    """
    ctx = CollectContext(cfg)
    host = Host(name=cfg.display, label=cfg.label,
                self_host=True, seen=time.time())
    host.cpu = system_mod.cpu()
    host.mem = system_mod.memory()
    host.disk = system_mod.disk()
    host.uptime = system_mod._uptime()
    host.net = system_mod.netio()
    if cfg.gpu:
        host.gpus = system_mod.gpus(True)

    wanted = only or names()
    errors = []
    for name in wanted:
        fn = get(name)
        if fn is None:
            continue
        try:
            _assign(host, name, fn(ctx))
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    if errors:
        host.alerts.append("collect: " + ", ".join(errors[:3]))

    _bind_sessions(host)
    host.seen = time.time()
    return Snapshot(host=host, collected=host.seen)


#: A session older than this is not the live run of a process, it is history.
_SESSION_FRESH = 300.0


def _bind_sessions(host: Host) -> None:
    """Copy model and tool hints from transcripts onto the live processes.

    The process table knows that a ``claude`` is running; only the transcript
    knows it is on opus. Without this join the MODEL column is permanently
    ``-`` and the panel's most expensive column carries no information at all.

    Matching is deliberately conservative -- same project, session younger than
    five minutes, one agent per session. Guessing here would put the wrong
    model's name next to the wrong process, which is worse than a dash, and a
    dash is at least visibly an absence.
    """
    if not host.sessions or not host.agents:
        return
    fresh = [s for s in host.sessions if s.age <= _SESSION_FRESH]
    fresh.sort(key=lambda s: s.age)
    taken: set[int] = set()
    for a in host.agents:
        if a.model:
            continue
        for i, s in enumerate(fresh):
            if i in taken or not s.model:
                continue
            # `project` on the agent is the cwd's last segment; on the session
            # it is the decoded project directory. Compare loosely -- the
            # substring test survives the case and separator differences
            # between the two, and a false match costs a model name on a row
            # that already said "-".
            pa, ps = (a.project or "").lower(), (s.project or "").lower()
            if pa and ps and (pa in ps or ps in pa):
                a.model = s.model
                if not a.work or a.work == a.kind:
                    a.work = s.last_tool or s.prompt or a.work
                taken.add(i)
                break


def _assign(host: Host, name: str, value: Any) -> None:
    if name == "system":
        for k, v in (value or {}).items():
            if k == "hostname" and not host.name:
                host.name = v
            elif hasattr(host, k):
                setattr(host, k, v)
    elif name == "agents":
        host.agents = list(value)
    elif name == "sessions":
        host.sessions = list(value)
    elif name == "ports":
        host.ports = {str(k): v for k, v in value.items()}
    elif name == "tmux":
        host.tmux = list(value)
    elif name == "pressure":
        # Not an alert. An empty todo list is the normal state of a machine
        # nobody has asked anything of yet, and the ATTENTION panel exists to
        # be trustworthy -- padding it with non-problems destroys that.
        host.pressure = dict(value) if isinstance(value, dict) else {}
    else:
        # User collectors land here and are surfaced by the generic panel.
        host.extra[name] = value
