"""Cross-platform system vitals.

Strategy, in order of preference:

1. ``psutil`` if it is installed -- it is the right answer everywhere.
2. Platform-native commands (``/proc``, ``vm_stat``, ``wmic``/``typeperf``,
   ``nvidia-smi``).
3. Nothing. The panel degrades to "unknown" instead of raising.

The fallback path exists because the package is stdlib-only by default and
because a dashboard that refuses to start on a machine lacking an optional
dependency is worse than one that shows slightly less. Every collector
returns a populated model object even on total failure -- a gap and a zero
look identical otherwise, and the difference matters.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..model import CPU, GPU, Disk, Host, Mem, NetIO

try:                                    # optional, but strongly preferred
    import psutil
except Exception:                       # pragma: no cover
    psutil = None

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

PROC = Path("/proc")


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return r.stdout or ""
    except Exception:
        return ""


# --- cpu ---------------------------------------------------------------------

#: Last ``cpu_times`` sample, so utilisation can be derived from a delta.
#:
#: ``psutil.cpu_percent(interval=None)`` returns a confident ``0.0`` on its very
#: first call, because it has nothing to compare against. On a dashboard whose
#: first frame is also its introduction, that reads as "this box is idle" --
#: the most confident kind of wrong.
_CPU_PREV: tuple | None = None


def cpu() -> CPU:
    global _CPU_PREV
    if psutil:
        try:
            load = list(getattr(os, "getloadavg", lambda: (0, 0, 0))())
            now = time.time()
            times = psutil.cpu_times(percpu=True)
            prev, _CPU_PREV = _CPU_PREV, (times, now)
            if prev is not None and len(prev[0]) == len(times) and now > prev[1]:
                span = now - prev[1]
                pcts = []
                for a, b in zip(times, prev[0]):
                    dt = ((a.user + a.system) - (b.user + b.system)) / span
                    pcts.append(max(0.0, min(100.0, 100.0 * dt)))
                return CPU(pct=sum(pcts) / len(pcts), cores=pcts, load=load)
            # First sample: pay 150ms for a real number rather than a wrong one.
            p = psutil.cpu_percent(interval=0.15, percpu=True)
            return CPU(pct=sum(p) / len(p) if p else 0.0, cores=list(p),
                       load=load)
        except Exception:
            pass
    if not IS_WIN and PROC.is_dir():
        return _cpu_proc()
    if IS_MAC:
        return _cpu_mac()
    return _cpu_win()


def _cpu_proc() -> CPU:
    cores: list[float] = []
    with open(PROC / "stat", encoding="utf-8") as fh:
        parts = fh.readline().split()
    total = sum(float(x) for x in parts[1:])
    idle = float(parts[4]) + (float(parts[5]) if len(parts) > 5 else 0.0)

    def snap() -> tuple[float, float]:
        with open(PROC / "stat", encoding="utf-8") as fh:
            p = fh.readline().split()
        return sum(float(x) for x in p[1:]), float(p[4]) + float(p[5])

    t0, i0 = snap()
    time.sleep(0.12)
    t1, i1 = snap()
    dt, di = t1 - t0, i1 - i0
    pct = 100.0 * (1.0 - di / dt) if dt > 0 else 0.0

    # Per-core needs a second read of /proc/stat's per-cpu lines.
    cores = []
    try:
        def raw() -> list[tuple[float, float]]:
            out = []
            with open(PROC / "stat", encoding="utf-8") as fh:
                for line in fh:
                    if not line.startswith("cpu") or line.startswith("cpu "):
                        continue
                    f = line.split()
                    out.append((sum(float(x) for x in f[1:]),
                                float(f[4]) + float(f[5])))
            return out
        a, b = raw(), raw()
        for (t0c, i0c), (t1c, i1c) in zip(a, b):
            d = t1c - t0c
            cores.append(max(0.0, min(100.0, 100.0 * (1.0 - (i1c - i0c) / d))) if d > 0 else 0.0)
    except Exception:
        cores = []

    try:
        load = list(os.getloadavg())
    except Exception:
        load = []
    return CPU(pct=pct, cores=cores, load=load)


def _cpu_mac() -> CPU:
    out = _run(["ps", "-A", "-o", "%cpu"])
    vals = []
    for line in out.splitlines()[1:]:
        try:
            vals.append(float(line.strip()))
        except ValueError:
            pass
    try:
        load = list(os.getloadavg())
    except Exception:
        load = []
    return CPU(pct=sum(vals) / len(vals) if vals else 0.0, cores=vals, load=load)


def _cpu_win() -> CPU:
    # typeperf gives one counter per logical processor; a single global
    # counter is not enough to build the per-core strip the panel wants.
    n = os.cpu_count() or 1
    counters = [f"\\Processor({i})\\% Processor Time" for i in range(n)]
    out = _run(["typeperf", "-sc", "1"] + counters, timeout=4.0)
    vals = []
    for line in out.splitlines():
        for tok in re.findall(r"\(?\d[\d,]*\)?", line):
            try:
                vals.append(float(tok.replace(",", "").strip("()")))
            except ValueError:
                pass
    if not vals:
        return CPU(pct=0.0, cores=[], load=[])
    vals = vals[:n]
    return CPU(pct=sum(vals) / len(vals), cores=vals, load=[])


# --- memory ------------------------------------------------------------------

def memory() -> Mem:
    if psutil:
        try:
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            return Mem(total=vm.total, avail=vm.available,
                       swap_total=sw.total, swap_used=sw.used)
        except Exception:
            pass
    if IS_WIN:
        return _mem_win()
    if IS_MAC:
        return _mem_mac()
    return _mem_proc()


def _mem_proc() -> Mem:
    info = {}
    try:
        with open(PROC / "meminfo", encoding="utf-8") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                info[k] = int(rest.split()[0]) * 1024
    except Exception:
        return Mem()
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return Mem(total=total, avail=avail,
               swap_total=info.get("SwapTotal", 0),
               swap_used=info.get("SwapTotal", 0) - info.get("SwapFree", 0))


def _mem_win() -> Mem:
    total = avail = 0
    try:
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(st))
        total, avail = int(st.ullTotalPhys), int(st.ullAvailPhys)
    except Exception:
        pass
    return Mem(total=total, avail=avail)


def _mem_mac() -> Mem:
    out = _run(["vm_stat"])
    page = 4096
    stats = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            stats[k.strip()] = v.strip().rstrip(".")
    try:
        total = int(stats["Pages total"]) * page
        free = int(stats["Pages free"]) * page
        spec = int(stats.get("Pages purgeable", 0)) * page
        avail = free + spec
    except Exception:
        return Mem()
    return Mem(total=total, avail=avail)


# --- disk --------------------------------------------------------------------

def disk(path: str | None = None) -> Disk:
    path = path or (os.environ.get("SystemDrive", "C:\\") + "\\" if IS_WIN else "/")
    try:
        if psutil:
            u = psutil.disk_usage(path)
            return Disk(mount=path, total=u.total, used=u.used)
        st = shutil.disk_usage(path)
        return Disk(mount=path, total=st.total, used=st.total - st.free)
    except Exception:
        return Disk(mount=path)


# --- network -----------------------------------------------------------------

_NET_RE = re.compile(r"^\s*([^:]+):\s+(.*)$")


def netio() -> NetIO:
    rx = tx = 0
    try:
        if psutil:
            n = psutil.net_io_counters()
            return NetIO(rx=n.bytes_recv, tx=n.bytes_sent)
        if not IS_WIN and PROC.is_dir():
            with open(PROC / "net/dev", encoding="utf-8") as fh:
                for line in fh.readlines()[2:]:
                    m = _NET_RE.match(line)
                    if not m or m.group(1).strip() == "lo":
                        continue
                    f = m.group(2).split()
                    rx += int(f[0])
                    tx += int(f[8])
            return NetIO(rx=rx, tx=tx)
    except Exception:
        pass
    return NetIO(rx=rx, tx=tx)


# --- gpu ---------------------------------------------------------------------

def gpus(enabled: bool = True) -> list[GPU]:
    if not enabled:
        return []
    out = _nvidia() or _rocm()
    if not out and IS_MAC:
        try:
            out = _apple_gpu()
        except Exception:
            out = []
    return out


def _nvidia() -> list[GPU]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, "--query-gpu=index,name,utilization.gpu,memory.used,"
                  "memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, errors="replace")
        out = []
        for line in r.stdout.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) < 7:
                continue
            out.append(GPU(idx=int(float(f[0])), name=f[1], pct=float(f[2]),
                           vram_used=int(float(f[3])), vram_total=int(float(f[4])),
                           temp=int(float(f[5])), power=float(f[6])))
        return out
    except Exception:
        return []


def _rocm() -> list[GPU]:
    exe = shutil.which("rocm-smi")
    if not exe:
        return []
    raw = _run([exe, "--showuse", "--showmemuse", "--showtemp",
                "--showproductname", "--json"], timeout=5)
    try:
        d = json.loads(raw)
    except Exception:
        return []
    out = []
    for card, v in d.items():
        try:
            out.append(GPU(idx=int(card.replace("card", "").strip()),
                           name=v.get("Card series", "gpu"),
                           pct=float(v.get("GPU use (%)", 0))))
        except Exception:
            continue
    return out


def _apple_gpu() -> list[GPU]:
    raw = _run(["ioreg", "-l", "-d", "1"], timeout=5)
    m = re.search(r'"VRAM,totalBytes" = (\d+)', raw)
    total = int(m.group(1)) if m else 0
    return [GPU(name="Apple GPU", vram_total=total)] if total else []


# --- host identity -----------------------------------------------------------

def host_identity(cfg) -> dict:
    """The parts of a snapshot that describe the machine rather than its load.

    Returned as a plain dict rather than a :class:`Host` because ``collect()``
    already owns the record; this fills in the identity fields that no
    collector would otherwise know about.
    """
    return {
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.version()[:60],
        "hostname": platform.node(),
    }


def _uptime() -> float:
    try:
        if psutil:
            return time.time() - psutil.boot_time()
        if IS_WIN:
            return time.time() - _win_boot()
        return time.time() - os.stat("/proc/1").st_ctime
    except Exception:
        return 0.0


def _win_boot() -> float:
    import ctypes
    from ctypes import wintypes

    class FT(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    k = ctypes.WinDLL("kernel32")
    k.SystemTimeToFileTime.argtypes = [wintypes.SYSTEMTIME,
                                       ctypes.POINTER(FT)]
    st = wintypes.SYSTEMTIME()
    k.GetSystemTime(ctypes.byref(st))
    ft = FT()
    k.SystemTimeToFileTime(ctypes.byref(st), ctypes.byref(ft))
    ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    # FILETIME counts 100ns intervals since 1601.
    return (ticks / 1e7) - 11644473600.0
