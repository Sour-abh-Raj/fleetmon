"""The data model.

These are plain dataclasses with ``to_dict``/``from_dict``, which is what makes
the multi-device view possible: a snapshot produced on any machine serialises
to JSON and is consumed identically whether it arrived over ssh, a shared
directory or HTTP.

Field names are short because a snapshot of a busy fleet is tens of kilobytes
and travels every second. ``a`` is an agent, ``h`` is a host.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = 3          # bump when the wire format changes incompatibly


@dataclass
class CPU:
    """Per-core utilisation, in percent."""
    pct: float = 0.0
    cores: list[float] = field(default_factory=list)
    load: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.cores)


@dataclass
class Mem:
    total: int = 0
    avail: int = 0
    swap_total: int = 0
    swap_used: int = 0

    @property
    def used(self) -> int:
        return max(0, self.total - self.avail)

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


@dataclass
class Disk:
    mount: str = "/"
    total: int = 0
    used: int = 0

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


@dataclass
class GPU:
    idx: int = 0
    name: str = "gpu"
    pct: float = 0.0
    vram_used: int = 0
    vram_total: int = 0
    temp: int = 0
    power: float = 0.0

    @property
    def vram_pct(self) -> float:
        return 100.0 * self.vram_used / self.vram_total if self.vram_total else 0.0


@dataclass
class NetIO:
    rx: int = 0          # cumulative bytes
    tx: int = 0


@dataclass
class Host:
    name: str = "host"
    label: str = ""
    self_host: bool = False
    seen: float = 0.0    # unix time the snapshot was taken
    age: float = 0.0     # seconds since then, filled in at render
    os: str = ""
    kernel: str = ""
    uptime: float = 0.0
    cpu: CPU = field(default_factory=CPU)
    mem: Mem = field(default_factory=Mem)
    disk: Disk = field(default_factory=Disk)
    gpus: list[GPU] = field(default_factory=list)
    net: NetIO = field(default_factory=NetIO)
    agents: list["Agent"] = field(default_factory=list)
    sessions: list["Session"] = field(default_factory=list)
    ports: dict[str, "Port"] = field(default_factory=dict)
    tmux: list["TmuxPane"] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    #: Todo/checkpoint counters. Not an alert -- an empty list is normal.
    pressure: dict[str, Any] = field(default_factory=dict)
    #: Results of user-registered collectors, keyed by collector name.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.label or self.name


@dataclass
class Agent:
    """One running agentic process."""
    pid: int = 0
    kind: str = "claude"          # claude | idea-loop | humanizer | custom...
    model: str = ""
    project: str = ""
    work: str = ""                # one-line description of what it is doing
    cwd: str = ""
    cpu: float = 0.0
    rss: int = 0               # resident set, megabytes (normalised at collect)
    age: float = 0.0
    bound: bool = True            # False if we only see the transcript
    state: str = ""               # tool name, if the transcript names one
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class Session:
    """A transcript-level agent session, which may outlive its process."""
    project: str = ""
    model: str = ""
    msgs: int = 0
    age: float = 0.0
    tools: int = 0
    last_tool: str = ""
    prompt: str = ""
    sid: str = ""
    live: bool = False


@dataclass
class Port:
    up: bool = False
    label: str = ""
    note: str = ""
    detail: str = ""
    latency: float = 0.0
    since: float = 0.0
    slots: int = 0
    slots_busy: int = 0


@dataclass
class TmuxPane:
    session: str = ""
    window: str = ""
    cmd: str = ""
    pid: int = 0
    cwd: str = ""
    dead: bool = False
    agent: str = ""


@dataclass
class Snapshot:
    """One machine's complete state at one instant."""
    host: Host = field(default_factory=Host)
    schema: int = SCHEMA
    collected: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "collected": self.collected,
                "host": _enc(self.host)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Snapshot":
        # ``expect`` matters here: an empty or unrecognisable host payload has
        # no marker to guess from, and without the hint the caller gets a bare
        # dict and the first attribute access raises somewhere far away.
        return cls(host=_dec(d.get("host") or {}, Host),
                   schema=d.get("schema", 1),
                   collected=d.get("collected", 0.0))

    # -- encode/decode -------------------------------------------------------

    def __json__(self) -> str:
        import json
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def loads(cls, raw: str | bytes) -> "Snapshot":
        import json
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return cls.from_dict(json.loads(raw))


def _enc(o: Any) -> Any:
    """Encode to wire form, tagging every dataclass with its type.

    The tag is not optional bookkeeping. Guessing the type from the field
    names works until one dataclass grows a field that collides with another's
    -- ``Host`` has ``cpu``, and ``CPU`` is itself a wire type, so an untagged
    host decodes as a CPU and every consumer silently reads zeros.
    """
    if hasattr(o, "__dataclass_fields__"):
        name = type(o).__name__.lower()
        return {"_t": name,
                **{f.name: _enc(getattr(o, f.name)) for f in _fields(o)}}
    if isinstance(o, dict):
        # A mapping *of* dataclasses gets its own tag, otherwise the decoder
        # sees ``{"8080": {...}}`` and has to guess between "this is a Port"
        # and "this is a dict of Ports" from the contents alone.
        if o and all(hasattr(v, "__dataclass_fields__") for v in o.values()):
            return {"_t": MAP, "items": {str(k): _enc(v) for k, v in o.items()}}
        return {str(k): _enc(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_enc(v) for v in o]
    return o


_TYPES = {
    "host": Host, "cpu": CPU, "mem": Mem, "disk": Disk, "gpu": GPU,
    "netio": NetIO, "agent": Agent, "session": Session, "port": Port,
    "tmuxpane": TmuxPane,
}

#: Sentinel tag for "a dict whose values are records", not a record itself.
MAP = "__map__"

#: One field that only this type has. Used to decode untagged payloads from an
#: older build, so a mixed-version fleet degrades to "mostly right" rather than
#: to "silently wrong".
_MARKER = {
    "host": "self_host", "cpu": "cores", "mem": "avail", "disk": "mount",
    "gpu": "vram_total", "netio": "rx", "agent": "bound", "session": "msgs",
    "port": "label", "tmuxpane": "window",
}


def _guess(d: dict[str, Any]):
    for name, marker in _MARKER.items():
        if marker in d:
            return _TYPES[name]
    return None


def _dec(d: Any, expect: type | None = None) -> Any:
    """Rebuild dataclasses from wire dicts, tolerating unknown keys.

    Forward and backward tolerance matters: a fleet is by definition several
    machines at possibly different versions, and an old collector must not be
    unable to render just because a newer one grew a field.

    ``expect`` is the field's declared type. It is the fallback when the payload
    carries no ``_t`` tag and no marker field is present -- an empty host dict
    has nothing to guess from, but the caller still knows a ``Host`` belongs
    there and would rather get a blank one than a ``dict``.
    """
    if isinstance(d, list):
        return [_dec(v) for v in d]
    if not isinstance(d, dict):
        return d
    tag = str(d.get("_t", "")).lower()
    if tag == MAP:
        return {k: _dec(v) for k, v in (d.get("items") or {}).items()}
    cls = _TYPES.get(tag) if tag else (_guess(d) or expect)
    if cls is None:
        return {k: _dec(v) for k, v in d.items()}
    kwargs = {}
    for f in _fields(cls):
        if f.name in d:
            kwargs[f.name] = _dec(d[f.name], _field_type(cls, f.name))
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()


def _field_type(cls, name: str):
    """The declared type of one field, or None if it is not a simple lookup.

    ``from __future__ import annotations`` puts every annotation in a string,
    so this reads the source rather than ``__annotations__`` -- which is exactly
    why the nested decode gets a real class to fall back on and not the string
    ``"Host"``.
    """
    import dataclasses
    import typing
    f = next((x for x in _fields(cls) if x.name == name), None)
    if f is None:
        return None
    t = f.type
    if isinstance(t, str):
        t = typing.get_type_hints(cls).get(name, t)  # noqa: F821
    if isinstance(t, type) and dataclasses.is_dataclass(t):
        return t
    return None


def _fields(cls):
    import dataclasses
    return dataclasses.fields(cls)


# --- helpers -----------------------------------------------------------------

def human_age(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = float(sec)
    if sec < 0:
        return "now"
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{int(sec // 3600)}h{int((sec % 3600) // 60):02d}"
    return f"{int(sec // 86400)}d{int((sec % 86400) // 3600):02d}"


def human_n(n: float) -> str:
    n = float(n)
    for unit, div in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}"


def human_mb(kb: float) -> str:
    return f"{kb / 1024:.0f}M" if kb < 1024 * 1024 else f"{kb / 1048576:.1f}G"


def human_bytes(n: float) -> str:
    return human_n(n)


def now() -> float:
    return time.time()


def freshness(host: Host, ref: float | None = None) -> float:
    """Seconds since this host's snapshot was taken.

    A host that has gone quiet is the single most important thing a fleet view
    can tell you -- a monitor that silently renders a dead machine as healthy
    is worse than no monitor at all.
    """
    return max(0.0, (ref or time.time()) - (host.seen or time.time()))
