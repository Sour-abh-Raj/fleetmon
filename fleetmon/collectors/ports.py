"""Listening ports and inference-server health probes.

Two separate questions, kept separate on purpose:

*Is anything listening?* Answered by the kernel -- fast, no network, works
identically on all three platforms.

*Is it healthy and what is it?* Answered by HTTP against ``/health``,
``/props``, ``/metrics`` or ``/slots``. Slow, and only meaningful for servers
that speak HTTP.

Conflating them is how a dashboard ends up showing four "DOWN" rows for ports
that were never meant to run on this machine. The ``hosts_llm`` latch below
exists specifically to prevent that: a host that has never hosted inference
simply has no server rows, rather than a column of false alarms.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..model import Port

IS_WIN = False
try:
    import os
    IS_WIN = os.name == "nt"
except Exception:                          # pragma: no cover
    pass

# Ports worth watching by default. Overridable per-machine in the config.
DEFAULT_SERVERS: list[tuple[int, str, str]] = [
    (8000, "laya-judge", "grounding judge for the idea loop"),
    (8082, "humanizer", "authormist-q4km, CUDA offload"),
    (8083, "loop-engine", "qwen2.5-7b-q3, CPU"),
    (11434, "ollama", "ollama daemon"),
]

PROBES = ["/health", "/props", "/v1/models", "/metrics", "/slots"]


def listening() -> set[int]:
    """Ports in LISTEN state, read from the kernel."""
    if IS_WIN:
        return _listen_win()
    return _listen_proc() or _listen_sysctl()


def _listen_proc() -> set[int]:
    out: set[int] = set()
    try:
        with open("/proc/net/tcp", encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) > 3 and f[3] == "0A":       # TCP_LISTEN
                    out.add(int(f[1].split(":")[1], 16))
    except OSError:
        pass
    try:
        with open("/proc/net/tcp6", encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) > 3 and f[3] == "0A":
                    out.add(int(f[1].split(":")[1], 16))
    except OSError:
        pass
    return out


def _listen_sysctl() -> set[int]:
    import subprocess
    try:
        r = subprocess.run(["netstat", "-an"], capture_output=True, text=True,
                           timeout=6, errors="replace")
    except Exception:
        return set()
    out = set()
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) > 3 and f[0].endswith("tcp") and "LISTEN" in line.upper():
            try:
                out.add(int(f[f.index("." + f[-1]) if False else -1].split(".")[-1]))
            except (ValueError, IndexError):
                continue
    return out


def _listen_win() -> set[int]:
    """Listening TCP ports via iphlpapi, which is fast where WMI is not.

    The constants matter: AF_INET is 2, and the class that actually returns
    owning PIDs is MIB_TCP_TABLE_OWNER_PID_ALL (5). Passing the wrong family
    does not raise -- it returns an empty table, which is how this ends up
    silently reporting "no ports" on every Windows box.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class MIBTCPROW_PID(ctypes.Structure):
            _fields_ = [("dwState", wintypes.DWORD),
                        ("dwLocalAddr", wintypes.DWORD),
                        ("dwLocalPort", wintypes.DWORD),
                        ("dwRemoteAddr", wintypes.DWORD),
                        ("dwRemotePort", wintypes.DWORD),
                        ("dwOwningPid", wintypes.DWORD)]

        AF_INET = 2
        TCP_TABLE_OWNER_PID_ALL = 5
        MIB_TCP_STATE_LISTEN = 2

        ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
        ip.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int]
        ip.GetExtendedTcpTable.restype = wintypes.DWORD

        size = wintypes.DWORD(0)
        rc = ip.GetExtendedTcpTable(None, ctypes.byref(size), True,
                                    AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if rc != 0 or not size.value:
            return _listen_netstat_win()
        buf = ctypes.create_string_buffer(size.value)
        rc = ip.GetExtendedTcpTable(buf, ctypes.byref(size), True,
                                    AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if rc != 0:
            return _listen_netstat_win()
        n = size.value // ctypes.sizeof(MIBTCPROW_PID)
        rows = (MIBTCPROW_PID * n).from_buffer(buf)
        out = {int(rows[i].dwLocalPort) for i in range(n)
               if rows[i].dwState == MIB_TCP_STATE_LISTEN}
        return out or _listen_netstat_win()
    except Exception:
        return _listen_netstat_win()


def _listen_netstat_win() -> set[int]:
    import subprocess
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                           capture_output=True, text=True, timeout=8,
                           errors="replace")
    except Exception:
        return set()
    out = set()
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0].upper() == "TCP" and "LISTENING" in line.upper():
            try:
                out.add(int(f[1].rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                continue
    return out


# --- probing -----------------------------------------------------------------

def probe(port: int, timeout: float = 1.2) -> Port:
    """HTTP health probe, falling back to a bare TCP connect."""
    p = Port(up=False)
    for path in PROBES:
        t0 = time.time()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                        timeout=timeout) as r:
                body = r.read(65536).decode("utf-8", "replace")
            p.up = True
            p.latency = time.time() - t0
            p.detail = _describe(path, body)
            return p
        except urllib.error.HTTPError as e:
            # A 404 still proves something is there and speaking HTTP.
            p.up = True
            p.latency = time.time() - t0
            p.detail = f"HTTP {e.code}"
            return p
        except Exception:
            continue
    t0 = time.time()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            p.up = True
            p.latency = time.time() - t0
            p.detail = "tcp"
            return p
    except Exception:
        return p


def _describe(path: str, body: str) -> str:
    """Pull the one useful number out of whichever endpoint answered."""
    if path == "/props" or path == "/v1/models":
        try:
            d = json.loads(body)
        except Exception:
            return ""
        if isinstance(d, dict):
            if "data" in d and isinstance(d["data"], list):
                names = [m.get("id", "") for m in d["data"][:3] if isinstance(m, dict)]
                return ", ".join(n for n in names if n)
            for k in ("model_path", "default_generation_settings"):
                if k == "model_path" and isinstance(d.get(k), str):
                    return d[k].split("/")[-1]
            if isinstance(d.get("model"), str):
                return d["model"].split("/")[-1]
    if path == "/slots":
        try:
            d = json.loads(body)
            if isinstance(d, list):
                busy = sum(1 for s in d if isinstance(s, dict) and s.get("is_processing"))
                return f"{busy}/{len(d)} busy"
        except Exception:
            pass
    if path == "/metrics":
        out = []
        for key in ("prompt_tokens_total", "tokens_predicted_total"):
            for line in body.splitlines():
                if line.startswith(key):
                    try:
                        out.append(f"{key.split('_')[0]} {int(float(line.split()[-1]))}")
                    except (ValueError, IndexError):
                        pass
        return " ".join(out)
    if path == "/health":
        return "healthy"
    return ""


class Net:
    """Port monitor with the phantom-row latch."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.servers = list(DEFAULT_SERVERS)
        extra = getattr(cfg, "servers", None)
        if extra:
            self.servers = [tuple(s) for s in extra]
        self.timeout = cfg.probe_timeout
        self.data: dict[int, Port] = {}
        # Sticky: set once any probe succeeds here. Until then an absent port
        # is not news, it is a machine with nothing to serve.
        self.hosts_llm = False
        self._since: dict[int, float] = {}

    def probe_all(self, servers=None) -> dict[int, Port]:
        servers = servers or self.servers
        live = self.listening()
        if not self.cfg.probe_ports:
            return {p: Port(up=p in live, label=l, note=n) for p, l, n in servers}

        wait = [s for s in servers if s[0] in live]
        if wait:
            self.hosts_llm = True
        if not self.hosts_llm:
            # No inference here and never has been. Returning an empty map is
            # the honest answer; four DOWN rows would be a lie.
            return {}

        out: dict[int, Port] = {}
        if wait:
            with ThreadPoolExecutor(max_workers=min(8, len(wait))) as pool:
                res = list(pool.map(lambda s: probe(s[0], self.timeout), wait))
            for (port, label, note), p in zip(wait, res):
                p.label, p.note = label, note
                p.since = self._since.setdefault(port, time.time()) \
                    if p.up else 0.0
                if not p.up:
                    self._since.pop(port, None)
                out[port] = p

        for port, label, note in servers:
            if port not in out and port not in live and self.hosts_llm:
                out[port] = Port(up=False, label=label, note=note)
        self.data = out
        return out

    def listening(self) -> set[int]:
        return listening()
