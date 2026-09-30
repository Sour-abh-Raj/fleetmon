"""Shared test fixtures.

Fake hosts exist so the layout tests never touch the machine they run on. A
test that passes because the laptop happens to have six agents running, and
fails in CI because it has none, is worse than no test.
"""

from __future__ import annotations

import time

from fleetmon.model import (
    Agent, CPU, Disk, GPU, Host, Mem, NetIO, Port, Session, TmuxPane,
)


def fake_host(name: str, *, agents: int = 3, self_host: bool = False,
              busy: bool = False, down_port: str = "", seen: float = 0.0) -> Host:
    """A host with nothing wrong about it unless a test says otherwise.

    ``down_port`` names a port that should be reported as down; ``seen`` is the
    timestamp, and the default of "now" is what keeps these hosts out of the
    staleness path unless a test is specifically about it.
    """
    return Host(
        name=name,
        label="",
        self_host=self_host,
        seen=seen or time.time(),
        os="Linux 6.9.0",
        kernel="6.9.0-test",
        uptime=86400.0,
        cpu=CPU(pct=45.0 if busy else 8.0,
                cores=[12.0, 88.0] + [3.0] * 6,
                load=[1.2, 0.9, 0.8]),
        mem=Mem(total=16 << 30, avail=6 << 30, swap_total=8 << 30),
        disk=Disk(mount="/", total=512 << 30, used=300 << 30),
        net=NetIO(rx=123456789, tx=9876543),
        agents=[Agent(pid=1000 + i, kind="claude" if i % 2 else "mcp",
                      model="sonnet", project=f"proj{i}",
                      work="running tests" if busy else "idle",
                      cpu=1.5 if busy else 0.0, rss=100 + i,
                      age=60.0 * i, bound=True)
                for i in range(agents)],
        sessions=[Session(project=f"proj{i}", model="sonnet", msgs=i + 1,
                          age=30.0 * i, last_tool="Bash")
                  for i in range(min(agents, 4))],
        ports={k: Port(up=(k != down_port), label=lbl,
                       detail="1 model", latency=0.002)
                for k, lbl in (("8000", "ollama"), ("8080", "http"))},
        tmux=[TmuxPane(session="fleet", window=f"{i}", cmd="claude", pid=20 + i)
              for i in range(2)],
        gpus=[GPU(idx=0, name="RTX 4090", pct=50.0 if busy else 0.0,
                  vram_used=2 << 30, vram_total=24 << 30, temp=62)],
    )


def fake_hosts(n: int = 2, **kw) -> list[Host]:
    return [fake_host(f"host{i}", self_host=(i == 0), **kw) for i in range(n)]