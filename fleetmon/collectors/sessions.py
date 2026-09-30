"""Claude Code transcript parsing.

A transcript under ``~/.claude/projects/<encoded-cwd>/<session>.jsonl`` is the
only place that records *what an agent is thinking*, as opposed to merely that
it exists. It is append-only, so the last few lines are the newest activity
and reading them is cheap.

Two things make this non-trivial:

**Encoding.** The directory name is the cwd with separators replaced by
dashes, which makes the original unrecoverable for paths containing dashes.
``-home-sourabh-my-agent`` is ambiguous in principle; in practice the drive
form (``C--Users-sourah``) has a recognisable doubled dash after the drive
letter, which is enough to decode the overwhelmingly common case correctly.

**Cost.** A long-running session's transcript can be tens of megabytes. The
reader walks backwards from the end under a byte budget, so a busy machine
with fifty sessions still parses in single-digit milliseconds.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..model import Session

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS = CLAUDE_DIR / "projects"

CHUNK = 65536


def decode_project(name: str) -> str:
    """Turn a transcript directory name back into a path, best effort."""
    if name.startswith("-"):
        return "/" + name.lstrip("-").replace("-", "/")
    # "C--Users-sourah" -> "C:\Users\sourah". The drive letter carries no
    # colon in the encoded form -- the doubled dash IS the separator -- so the
    # drive prefix is rebuilt first and only the tail's dashes are converted.
    if len(name) > 3 and name[0].isalpha() and name[1:3] == "--":
        return name[0] + ":\\" + name[3:].replace("-", "\\")
    return name


def proj_label(path: str) -> str:
    """A short display label for a project path.

    A project at the home directory shows as ``~`` wherever it is rooted, not
    just under ``/home`` -- otherwise the Windows half of a fleet renders as a
    column of ``soura`` while the Linux half reads as home-relative.
    """
    if not path:
        return "?"
    p = Path(path)
    home = Path.home()
    if path.rstrip("/\\") == str(home):
        return "~"
    if p.parent == home:
        return "~"
    return p.name or path


_USER_RE = re.compile(r'"(model|role|type)"\s*:\s*"([^"]+)"')
_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')
_TOOL_RE = re.compile(r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"')
_PROMPT_RE = re.compile(r'"content"\s*:\s*"([^"]{12,300})"')


class Transcripts:
    """Newest-first reader over ``~/.claude/projects``."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = PROJECTS
        self.max_files = cfg.max_transcripts
        self.budget = cfg.transcript_budget
        self.enabled = cfg.read_transcripts and self.root.is_dir()
        self._cache: dict[str, tuple[float, Session]] = {}

    def sessions(self) -> list[Session]:
        if not self.enabled:
            return []
        try:
            files = sorted(self.root.glob("*/*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        out: list[Session] = []
        now = time.time()
        for f in files[: self.max_files]:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            hit = self._cache.get(str(f))
            if hit and now - hit[0] < 1.5:
                out.append(hit[1])
                continue
            s = self._read(f)
            s.age = max(0.0, now - mtime)
            self._cache[str(f)] = (now, s)
            out.append(s)
        return out

    def _read(self, path: Path) -> Session:
        s = Session(project=proj_label(decode_project(path.parent.name)),
                    sid=path.stem)
        msgs = tools = 0
        model = last_tool = prompt = ""
        try:
            for raw in self._tail(path):
                msgs += 1
                if '"model"' in raw:
                    m = _MODEL_RE.search(raw)
                    if m:
                        model = m.group(1)
                if '"tool_use"' in raw or '"tool_result"' in raw:
                    tools += 1
                    t = _TOOL_RE.search(raw)
                    if t:
                        last_tool = t.group(1)
                if not prompt and '"role":"user"' in raw.replace(" ", ""):
                    m = _PROMPT_RE.search(raw)
                    if m:
                        prompt = _clean(m.group(1))
        except OSError:
            pass
        s.model = _short_model(model)
        s.msgs = msgs
        s.tools = tools
        s.last_tool = last_tool
        s.prompt = prompt[:120]
        return s

    def _tail(self, path: Path, budget: int | None = None):
        """Yield the last few JSONL lines, newest last, within a byte budget."""
        budget = budget if budget is not None else self.budget
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > budget:
                    fh.seek(size - budget)
                    fh.readline()          # discard the partial first line
                blob = fh.read()
        except OSError:
            return
        text = blob.decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield line


def _clean(s: str) -> str:
    s = re.sub(r"\\n", " ", s)
    s = re.sub(r"\\t", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _short_model(m: str) -> str:
    if not m:
        return ""
    m = m.split("/")[-1]
    if m.startswith("claude-"):
        parts = m.split("-")
        return "-".join(parts[:2]) if len(parts) > 2 else m
    return m


# --- claude state ------------------------------------------------------------

def projects_in_flight() -> list[tuple[str, str]]:
    """``(project, session id)`` for transcripts modified in the last 5 min."""
    if not PROJECTS.is_dir():
        return []
    now = time.time()
    out = []
    try:
        for d in PROJECTS.iterdir():
            if not d.is_dir():
                continue
            try:
                newest = max((f.stat().st_mtime for f in d.glob("*.jsonl")),
                             default=0)
            except OSError:
                continue
            if newest and now - newest < 300:
                out.append((proj_label(decode_project(d.name)), str(int(newest))))
    except OSError:
        pass
    return out


def todo_state() -> tuple[int, int]:
    """``(done, total)`` across every TodoWrite the user has on disk."""
    todos = Path.home() / ".claude" / "todos"
    if not todos.is_dir():
        return 0, 0
    done = total = 0
    try:
        for f in todos.glob("*.json"):
            try:
                items = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(items, list):
                for t in items:
                    if isinstance(t, dict):
                        total += 1
                        done += 1 if t.get("status") == "completed" else 0
    except OSError:
        pass
    return done, total
