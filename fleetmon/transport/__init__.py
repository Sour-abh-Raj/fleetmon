"""Transports: how one machine's snapshot reaches another.

Four mechanisms, one interface. Each exists because a real fleet needs it:

``file``   A shared or synced directory. Works with no network at all -- a
           Dropbox folder, a git repo, a NAS. The right answer when machines
           are on different networks and ssh is not available.

``ssh``    ``fleetmon --collect --label=x`` over ssh. Works everywhere ssh
           does, needs no open ports, and is what most people already have.

``http``   One machine runs ``fleetmon --serve`` and others poll it. The only
           option that crosses NAT without an inbound tunnel, and the fastest
           per frame because there is no process spawn.

``auto``   Try them in order and use the first that answers. The default,
           because a config that has to be edited per pair of machines does
           not survive contact with reality.

Transports are **pull-based by default**: a viewer asks a source for its
snapshot. Nothing is transmitted without an explicit request, and the source
computes its own state rather than the viewer describing what it wants. That
keeps the wire format stable regardless of which version either end runs.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..model import Snapshot

TIMEOUT = 15.0


class Transport:
    """Base class. Subclasses implement :meth:`fetch`."""

    name = "base"

    def fetch(self, host) -> Snapshot | None:      # pragma: no cover
        raise NotImplementedError

    def describe(self, host) -> str:
        return f"{self.name}:{host.target or host.name}"


class FileTransport(Transport):
    """Read and write snapshots in a shared directory."""

    name = "file"

    @staticmethod
    def _path(host) -> Path:
        """Where this peer's snapshot lives.

        ``target`` may be the file itself or the directory it lives in -- a
        directory is the common case, because that is what a synced folder is.
        Resolving it in one place keeps ``push`` and ``fetch`` from disagreeing,
        which they did: a peer configured with a directory could be *read* and
        never *written*, and the asymmetry only showed up on the publishing side.
        """
        path = _expand(host.target or f"~/fleetmon-inbox")
        if path.is_dir() or not path.suffix:
            path = path / f"{host.name or 'peer'}.json"
        return path

    def fetch(self, host) -> Snapshot | None:
        try:
            raw = self._path(host).read_bytes()
        except OSError:
            return None
        try:
            return Snapshot.loads(raw)
        except Exception:
            return None

    def push(self, host, snap: Snapshot) -> bool:
        path = self._path(host)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a reader on the other side must never see a
            # half-written JSON file, which would read as "peer is broken".
            tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            tmp.write_text(snap.__json__(), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            return False


class SshTransport(Transport):
    """Run a one-shot collection on the peer and read its JSON."""

    name = "ssh"

    def fetch(self, host) -> Snapshot | None:
        cmd = host.target or host.name
        label = host.label or host.name
        remote = (f"fleetmon --collect --label={shlex.quote(label)} "
                  f"--raw 2>/dev/null")
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=accept-new", cmd, remote]
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=host.timeout or TIMEOUT,
                               errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        return _parse(r.stdout)

    def push(self, host, snap: Snapshot) -> bool:
        """Deliver our snapshot to a peer, which watches a path for it."""
        cmd = host.target or host.name
        dest = _expand(f"~/.fleetmon/inbox/{host.name or 'peer'}.json")
        try:
            proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", cmd,
                 f"mkdir -p {shlex.quote(str(dest.parent))} && cat > {shlex.quote(str(dest))}"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True)
            proc.communicate(snap.__json__(), timeout=host.timeout or TIMEOUT)
            return proc.returncode == 0
        except Exception:
            return False


class HttpTransport(Transport):
    """GET a JSON snapshot from ``fleetmon --serve``."""

    name = "http"

    def fetch(self, host) -> Snapshot | None:
        base = _url(host.target or host.name)
        try:
            with urllib.request.urlopen(f"{base}/snapshot",
                                        timeout=host.timeout or TIMEOUT) as r:
                return Snapshot.loads(r.read())
        except Exception:
            return None


class AutoTransport(Transport):
    """Try each mechanism until one answers, then stick with it.

    The choice is sticky because a peer that intermittently fails over from
    ssh to http produces a dashboard that flickers between two different
    truths. Once a mechanism works it keeps being used until it fails twice
    in a row.
    """

    name = "auto"

    ORDER = (SshTransport, FileTransport, HttpTransport)

    def __init__(self):
        self._chosen: dict[str, Transport] = {}
        self._misses: dict[str, int] = {}

    def fetch(self, host) -> Snapshot | None:
        key = host.name
        cur = self._chosen.get(key)
        order = ([type(cur)] if cur else []) + \
                [t for t in self.ORDER if not cur or not isinstance(cur, t)]
        for cls in order:
            snap = cls().fetch(host)
            if snap is not None:
                self._chosen[key] = cls()
                self._misses.pop(key, None)
                return snap
        self._misses[key] = self._misses.get(key, 0) + 1
        if self._misses.get(key, 0) >= 2:
            self._chosen.pop(key, None)
        return None

    def describe(self, host) -> str:
        cur = self._chosen.get(host.name)
        return (f"{cur.name}:{host.target or host.name}" if cur
                else f"auto:{host.target or host.name}")


# --- helpers -----------------------------------------------------------------

def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _url(target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return target.rstrip("/")
    return f"http://{target.rstrip('/')}"


def _parse(raw: str) -> Snapshot | None:
    """Pull the JSON out of whatever the peer printed.

    A peer may emit a warning or a progress line before the payload, so the
    last JSON object wins rather than the first non-space character.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Snapshot.loads(raw)
    except Exception:
        pass
    start = raw.rfind("\n{")
    if start >= 0:
        try:
            return Snapshot.loads(raw[start + 1:])
        except Exception:
            pass
    start = raw.find("{")
    if start >= 0:
        try:
            return Snapshot.loads(raw[start:])
        except Exception:
            pass
    return None


REGISTRY = {
    "file": FileTransport,
    "ssh": SshTransport,
    "http": HttpTransport,
    "auto": AutoTransport,
}


def build(via: str, hosts):
    """One transport instance per configured host."""
    cls = REGISTRY.get(via, AutoTransport)
    return {h.name: cls() for h in hosts}


def watch_inbox(path: str, on_snapshot):
    """Watch a directory for peer snapshots and hand each to ``on_snapshot``.

    Polling rather than inotify: inotify is not available on Windows or on
    network filesystems, both of which are exactly where a shared inbox lives.

    Returns the ``threading.Event`` that stops it.
    """
    import threading

    p = _expand(path)
    stop = threading.Event()
    seen: dict[str, float] = {}

    def loop():
        while not stop.is_set():
            try:
                for f in p.glob("*.json"):
                    try:
                        mtime = f.stat().st_mtime
                    except OSError:
                        continue
                    if seen.get(f.name) == mtime:
                        continue
                    seen[f.name] = mtime
                    snap = Snapshot.loads(f.read_bytes())
                    on_snapshot(snap)
            except OSError:
                pass
            stop.wait(2.0)

    t = threading.Thread(target=loop, name="fleetmon-inbox", daemon=True)
    t.start()
    return stop
