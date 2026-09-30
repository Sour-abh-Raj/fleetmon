"""Agent and process discovery.

The hard part of this module is not listing processes -- it is deciding which
processes are *agents*. Three classes of false positive have to be excluded,
and each was found by looking at a real frame on a real machine:

**The Claude desktop app.** On Windows ``claude.exe`` under ``WindowsApps`` is
Electron, and every one of its helpers is a browser process. Without the
exclusion a laptop shows twelve "agents" that are Chrome.

**Shells.** An agent's parent shell is not the agent. A ``bash -c claude ...``
line matches the same regex as ``claude`` itself.

**Wrappers.** ``npx claude``, ``uv run claude``, ``node .../cli.js``. These
genuinely are agents and must be detected, so they are matched positively
rather than filtered.

Everything here is read-only and cheap. On Windows the expensive operation is
reading every process's full command line via WMI, so image names are
pre-filtered before the expensive query.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ..model import Agent

try:
    import psutil
except Exception:                        # pragma: no cover
    psutil = None

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# --- signatures --------------------------------------------------------------

# Long-running agents that are not the Claude CLI but are still "working".
# (label, regex) -- matched against the full command line.

#: A command-line word ends at whitespace, at a quote, or at the end of the
#: string. A backslash is deliberately *not* a terminator: `\claude\anything`
#: is a directory, not an executable, and accepting it meant every process whose
#: command line merely passed through a folder named `claude` was reported as an
#: agent -- which included this tool's own shells, whose working directory is
#: `AppData\Local\Temp\claude\...`.
#:
#: It is a zero-width lookahead, so appending it shifts no capture group. The
#: quote is written as hex because the obvious spelling collides with the
#: string delimiter.
END = r'(?=$|[\s"\x27])'

DEFAULT_AGENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # These use ``END`` rather than a bare ``$|\\s`` terminator. A quoted
    # Windows path -- ``"...\\WinGet\\Links\\claude.exe"`` -- ends in a quote,
    # so a command-line-terminated pattern never matched it. That is not an
    # edge case; it is how a launcher is normally started, and it made the
    # entire no-psutil path report zero agents on a machine running three.
    ("claude", re.compile(
        r"claude-code-[^ ]*[/\\]bin[/\\]claude"
        r"|[/\\]bin[/\\]claude(\.exe)?\b"
        r"|claude-code[/\\]cli\.js"
        r"|@anthropic-ai[/\\]claude-code"
        r"|(^|[\s/\\])claude(\.exe)?" + END)),
    ("codex", re.compile(r"(^|[\s/\\])codex(\.exe)?" + END
                         + r"|@openai[/\\]codex")),
    ("aider", re.compile(r"(^|[\s/\\])aider(\.exe)?" + END)),
    ("crush", re.compile(r"(^|[\s/\\])crush(\.exe)?" + END)),
    ("llm-server", re.compile(
        r"(llama-server|llama\.cpp|vllm|ollama|text-generation|sglang"
        r"|lm[ _-]?studio|localai|llamacpp)")),
    ("mcp", re.compile(
        r"(mcp[-_]server|@modelcontextprotocol|open-knowledge[\\/]dist|"
        r"memory_mcp_server|ai-memory)")),
    ("agent-browser", re.compile(r"(agent-browser|playwright|browser_use)")),
    ("notebook", re.compile(r"(jupyter-lab|jupyter-notebook|ipykernel)")),
]

# On Windows the string "claude" is not a reliable agent signal.
ELECTRON_RE = re.compile(r"--type=(utility|renderer|gpu-process|broker|crashpad)")
CLAUDE_APP_RE = re.compile(
    r"WindowsApps[\\/]Claude_|ChromeNativeHost|chrome-native-host")

# Shells whose command line contains an agent invocation but which are not one.
# Matched with ``search``, not ``match``: a shell whose path contains a space
# arrives quoted (``"C:\\Program Files\\Git\\bin\\bash.exe"``), so the name is
# preceded by a separator rather than by the start of the string, and an
# anchored match would never see it.
SHELL_RE = re.compile(r"(^|[\s/\\])(bash|sh|zsh|fish|pwsh|powershell|cmd|env)\b")

# Cheap pre-filter for Windows WMI queries.
CANDIDATE_NAMES = re.compile(
    r"(claude|node|python|python3|ollama|llama|vllm|lm[ _-]?studio|bun|"
    r"deno|bash|sh|zsh|pwsh|powershell|cmd|wsl|codex|aider)", re.I)

# Wrapper shapes whose *parent* is the real agent command.
WRAPPERS = re.compile(r"(^|[\s/\\])(npx|uv|uvx|node|python3?|deno|bun|"
                      r"poetry|pipx|dotenvx)($|\s)")

MODEL_RE = re.compile(
    r"--model[= ]+([\w.\-]+(?:/[\w.\-]+)*)"
    r"|claude-([a-z0-9]+(?:-[a-z0-9]+)*)"
    r"|\"model\"\s*:\s*\"([^\"]+)\"")


def build_patterns(extra: list[list[str]] | None = None):
    """Compile the detection table, letting the user extend it from config."""
    pats = list(DEFAULT_AGENT_PATTERNS)
    for item in extra or []:
        try:
            label, rx = item[0], item[1]
            pats.append((label, re.compile(rx)))
        except Exception:
            continue
    return pats


def classify(cmd: str, pats) -> str | None:
    """The agent label for a command line, or ``None`` if it is not an agent."""
    if not cmd:
        return None
    if ELECTRON_RE.search(cmd) or CLAUDE_APP_RE.search(cmd):
        return None
    for label, rx in pats:
        if rx.search(cmd):
            return label
    return None


def extract_model(cmd: str) -> str:
    m = MODEL_RE.search(cmd)
    if not m:
        return ""
    val = next((g for g in m.groups() if g), "")
    if not val:
        return ""
    val = val.strip().strip("\"'")
    if val.startswith("claude-"):
        # "claude-opus-4-5" reads worse in a 9-wide column than the family.
        parts = val.split("-")
        return "-".join(parts[:2]) if len(parts) > 2 else val
    if "/" in val:
        val = val.split("/")[-1]
    return val


# --- enumeration -------------------------------------------------------------

class Processes:
    """Live process inventory, refreshed on demand."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.pats = build_patterns(cfg.agent_patterns)
        self.ignore = cfg.ignore_cmd or []
        self._win_cache: tuple[float, dict[int, dict]] = (0.0, {})
        self._proc_cache: tuple[float, dict[int, dict]] = (0.0, {})
        self._prev_ct: dict[int, tuple[float, float]] = {}

    # -- public ---------------------------------------------------------------

    def agents(self) -> list[Agent]:
        out: list[Agent] = []
        now = time.time()
        for pid, p in self._raw():
            cmd = p.get("cmd") or ""
            if any(bad in cmd for bad in self.ignore):
                continue
            label = classify(cmd, self.pats)
            if label is None:
                continue
            if SHELL_RE.search(cmd) and not WRAPPERS.search(cmd):
                # A shell that merely mentions an agent. `bash -c claude ...`
                # is the wrapper case and is kept.
                if " -c " not in cmd and " /c " not in cmd:
                    continue
            created = p.get("create") or 0.0
            out.append(Agent(
                pid=pid,
                kind=label,
                model=extract_model(cmd),
                project=Path(p.get("cwd") or "").name,
                work=self._describe(label, cmd, p),
                cwd=p.get("cwd") or "",
                cpu=float(p.get("cpu", 0.0)),
                rss=int(p.get("rss", 0)),
                age=max(0.0, now - created) if created else 0.0,
                bound=True,
                state=p.get("state", ""),
            ))
        return out

    def all_procs(self, limit: int = 4000) -> dict[int, dict]:
        """Raw pid -> info, for user-supplied panels."""
        out = {}
        for pid, p in self._raw():
            out[pid] = p
            if len(out) >= limit:
                break
        return out

    def top(self, n: int = 8) -> list[dict]:
        return sorted(self.all_procs().values(),
                      key=lambda p: p.get("cpu", 0.0), reverse=True)[:n]

    # -- description ----------------------------------------------------------

    @staticmethod
    def _describe(label: str, cmd: str, p: dict) -> str:
        """A short, human phrase for what this agent is doing.

        Derived from the command line rather than invented: the most useful
        thing on the panel is the argument the user typed, trimmed to fit.
        """
        if label == "llm-server":
            m = re.search(r"--model[= ]([\w.\-/]+)", cmd)
            if m:
                return f"serving {m.group(1).split('/')[-1]}"
            return "serving"
        toks = [t for t in re.split(r"\s+", cmd.strip()) if t]
        skip = {"claude", "node", "python", "python3", "npx", "uv", "bash",
                "sh", "zsh", "pwsh", "cmd", "-y", "--yes"}
        for t in toks[1:]:
            if t in skip or t.startswith("-"):
                continue
            # Anything that looks like a filesystem path is the interpreter's
            # business, not the user's intent: on Windows the npm wrapper's
            # argv is mostly `C:\...\npm-cache\...`, and rendering that in the
            # WORKING ON column is noise, not information.
            if "/" in t or "\\" in t or re.match(r"^[A-Za-z]:", t):
                continue
            return t.strip("'\"")[:40]
        # Nothing but the interpreter and its own name on the command line --
        # which is the normal shape of a bare `claude` with no prompt argument.
        # Falling back to `label` prints "claude" in the WORKING ON column of a
        # row whose KIND already says claude, which is a column of noise. The
        # working directory is the one thing that distinguishes three otherwise
        # identical processes.
        cwd = (p.get("cwd") or "").rstrip("\\/")
        if cwd:
            return Path(cwd).name or cwd[-24:]
        return "idle"

    # -- backends -------------------------------------------------------------

    def _raw(self) -> list[tuple[int, dict]]:
        if psutil:
            return self._raw_psutil()
        if IS_WIN:
            return self._raw_wmi()
        if IS_MAC:
            return self._raw_ps()
        return self._raw_proc()

    def _raw_psutil(self) -> list[tuple[int, dict]]:
        # `cpu` is not a valid process_iter attribute -- psutil exposes CPU as
        # a method, not a field -- so it is derived here from the delta
        # between this sample's cumulative CPU time and the previous frame's.
        # That is one syscall per process per frame rather than a blocking
        # interval measurement, and it is what makes a 1s refresh honest.
        now = time.time()
        prev, self._prev_ct = self._prev_ct, {}
        out = []
        for pr in psutil.process_iter(["pid", "name", "cmdline",
                                       "memory_info", "create_time",
                                       "cwd", "status"]):
            try:
                info = pr.info
                ct = pr.cpu_times()
                total = float(getattr(ct, "user", 0.0)) + \
                    float(getattr(ct, "system", 0.0))
                created = info.get("create_time") or 0.0
                key = info["pid"]
                self._prev_ct[key] = (total, now)
                was = prev.get(key)
                cpu = 0.0
                if was and now > was[1]:
                    cpu = max(0.0, 100.0 * (total - was[0]) / (now - was[1]))
                cmd = " ".join(info.get("cmdline") or [])
                out.append((key, {
                    "cmd": cmd,
                    "name": info.get("name") or "",
                    "cpu": cpu,
                    "rss": (getattr(info.get("memory_info"), "rss", 0) or 0) >> 20,
                    "create": created,
                    "cwd": info.get("cwd") or "",
                    "state": info.get("status") or "",
                }))
            except Exception:
                continue
        return out

    def _raw_wmi(self) -> list[tuple[int, dict]]:
        """Win32_Process via WMI, cached briefly.

        The command-line query is the expensive part on Windows, and the
        dashboard asks for it once a second per host. A short cache keeps a
        multi-host view from issuing N queries per frame.
        """
        now = time.time()
        if now - self._win_cache[0] < 0.9:
            return list(self._win_cache[1].items())
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,Name,CommandLine,WorkingSetSize,CreationDate,"
            "KernelModeTime,UserModeTime | ConvertTo-Json -Compress"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=12, errors="replace")
            data = json.loads(r.stdout) if r.stdout.strip() else []
        except Exception:
            return list(self._win_cache[1].items())
        if isinstance(data, dict):
            data = [data]
        out: dict[int, dict] = {}
        hz = 1e7
        for d in data:
            pid = d.get("ProcessId")
            if not pid:
                continue
            name = d.get("Name") or ""
            cmd = d.get("CommandLine") or ""
            if not CANDIDATE_NAMES.search(name + " " + cmd):
                continue
            create = 0.0
            cd = d.get("CreationDate")
            if cd:
                try:
                    create = (time.mktime(time.strptime(
                        cd[:19] if isinstance(cd, str) else "", "%Y-%m-%dT%H:%M:%S"
                    )) if isinstance(cd, str) else 0.0)
                except Exception:
                    create = 0.0
            cpu = 0.0
            try:
                cpu = ((d.get("KernelModeTime") or 0) + (d.get("UserModeTime") or 0)) / hz
            except Exception:
                pass
            out[int(pid)] = {
                "cmd": cmd, "name": name,
                "rss": int(d.get("WorkingSetSize") or 0) >> 20,
                "cpu": cpu, "create": create, "cwd": "", "state": "",
            }
        self._win_cache = (now, out)
        return list(out.items())

    def _raw_ps(self) -> list[tuple[int, dict]]:
        out = _ps_aux()
        if not out:
            return []
        return [(int(p["pid"]), {
            "cmd": p["cmd"], "name": p["name"], "cpu": float(p["cpu"] or 0),
            "rss": float(p["rss"] or 0) // 1024,
            "create": now_from_etime(p.get("etime", 0)), "cwd": "", "state": "",
        }) for p in out if p.get("pid")]

    def _raw_proc(self) -> list[tuple[int, dict]]:
        """Linux without psutil: /proc, with the cost proportional to cmdline."""
        out = []
        tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        try:
            page = os.sysconf("SC_PAGE_SIZE")
        except Exception:
            page = 4096
        hz = _load_hz()
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                cmd = _read_cmd(entry.path)
                if not cmd or not CANDIDATE_NAMES.search(cmd):
                    continue
                stat = _read_stat(entry.path)
                rss = ((stat[23] * page) >> 20) if len(stat) > 23 else 0
                start = stat[21] if len(stat) > 21 else 0
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    name = fh.readline().split("(")[1].rsplit(")", 1)[0]
                create = (start / hz) if hz else 0.0
                state = _read_state(entry.path)
                cwd = _read_cwd(entry.path)
            except Exception:
                continue
            out.append((pid, {"cmd": cmd, "name": name, "cpu": 0.0,
                              "rss": rss, "create": create, "cwd": cwd,
                              "state": state}))
        return out


# --- /proc helpers -----------------------------------------------------------

def _read_cmd(path: str) -> str:
    try:
        with open(f"{path}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _read_stat(path: str) -> list[str]:
    try:
        with open(f"{path}/stat", encoding="utf-8") as fh:
            data = fh.read()
    except OSError:
        return []
    return data[data.rfind(")") + 2:].split()


def _read_state(path: str) -> str:
    try:
        with open(f"{path}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return line.split()[1]
    except OSError:
        pass
    return ""


def _read_cwd(path: str) -> str:
    try:
        return os.readlink(f"{path}/cwd")
    except OSError:
        return ""


_HZ: float | None = None


def _load_hz() -> float:
    global _HZ
    if _HZ:
        return _HZ
    try:
        _HZ = os.sysconf("SC_CLK_TCK")
    except Exception:
        _HZ = 100.0
    return _HZ


def _cpu_times() -> list[float]:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = fh.readline().split()
        return [float(x) for x in parts[1:]]
    except Exception:
        return []


def sample_cpu_delta(prev: list[float], cur: list[float]) -> float:
    """Percent CPU across all cores between two /proc/stat samples."""
    if not prev or not cur or len(prev) < 5:
        return 0.0
    dt = sum(cur) - sum(prev)
    idle = (cur[3] + (cur[4] if len(cur) > 4 else 0)) - \
           (prev[3] + (prev[4] if len(prev) > 4 else 0))
    return max(0.0, min(100.0, 100.0 * (1.0 - idle / dt))) if dt > 0 else 0.0


def now_from_etime(etime: float) -> float:
    """ps's etime (elapsed seconds) -> absolute creation time."""
    try:
        return time.time() - float(etime)
    except (TypeError, ValueError):
        return 0.0


def _ps_aux() -> list[dict]:
    import subprocess
    try:
        r = subprocess.run(["ps", "-axo", "pid,pcpu,rss,comm,args"],
                           capture_output=True, text=True, timeout=8,
                           errors="replace")
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:
        f = line.split(None, 4)
        if len(f) < 5:
            continue
        try:
            out.append({"pid": f[0], "cpu": f[1], "rss": f[2],
                        "name": f[3], "cmd": f[4]})
        except Exception:
            continue
    return out
