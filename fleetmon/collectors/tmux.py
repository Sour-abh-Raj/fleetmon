"""tmux session inventory.

tmux is the substrate a lot of agent work actually runs on, so a fleet view
that cannot see it is blind to where the work is happening. ``list-panes -a``
gives everything in one call, which matters when the same command has to run
against a dozen peers once a second.

The Windows host usually has no tmux at all; every function here returns an
empty list rather than raising, because a missing tmux is a normal state, not
an error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from ..model import TmuxPane

_FMT = (
    "#{session_name}\t#{window_index}#{window_name}\t#{pane_index}"
    "\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}"
)

_cache: tuple[float, list[TmuxPane]] = (0.0, [])


def available() -> bool:
    return bool(shutil.which("tmux"))


def panes(ttl: float = 0.9) -> list[TmuxPane]:
    global _cache
    now = time.time()
    if now - _cache[0] < ttl and _cache[1] is not None:
        return _cache[1]
    if not available():
        _cache = (now, [])
        return []
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", _FMT],
                           capture_output=True, text=True, timeout=5,
                           errors="replace")
    except Exception:
        _cache = (now, [])
        return []
    out: list[TmuxPane] = []
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        try:
            out.append(TmuxPane(
                session=f[0],
                window=f[1],
                cmd=f[4],
                pid=int(f[3]) if f[3].isdigit() else 0,
                cwd=f[5],
                dead=f[6].strip() == "1",
            ))
        except Exception:
            continue
    _cache = (now, out)
    return out


def sessions() -> list[str]:
    if not available():
        return []
    try:
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=5,
                           errors="replace")
        return [s for s in r.stdout.split() if s]
    except Exception:
        return []


def attach_hint() -> str:
    """The single most useful tmux command for whatever is running.

    Surfacing this is the difference between a dashboard you read and one you
    act on: the panel says what is busy, this says how to get to it.
    """
    for p in panes():
        if p.session and p.cmd and p.cmd not in ("bash", "zsh", "sh", "fish"):
            return f"tmux attach -t {p.session}"
    ss = sessions()
    return f"tmux attach -t {ss[0]}" if ss else ""
