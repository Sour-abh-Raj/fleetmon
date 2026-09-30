"""Wire-format tests.

These are the tests that matter most for a multi-device tool, because the
failure mode is not a crash -- it is a snapshot that decodes into a host with
zero CPUs and no agents, which renders perfectly and says nothing true.
"""

from __future__ import annotations

import json

from fleetmon.model import (
    SCHEMA, Agent, CPU, GPU, Host, Mem, Port, Session, Snapshot, TmuxPane,
)


def sample() -> Host:
    return Host(
        name="box", label="Box", self_host=True, os="Linux 6.9", uptime=1234.5,
        cpu=CPU(pct=42.5, cores=[10.0, 90.0], load=[1.0, 0.5, 0.4]),
        mem=Mem(total=1000, avail=250, swap_total=100, swap_used=10),
        agents=[Agent(pid=7, kind="claude", model="sonnet", cpu=1.5, rss=99)],
        sessions=[Session(project="proj", model="sonnet", msgs=4, age=9.0)],
        ports={"8080": Port(up=True, label="api", latency=0.01)},
        tmux=[TmuxPane(session="fleet", window="0", cmd="claude", pid=3)],
        gpus=[GPU(idx=0, name="RTX", pct=30.0, vram_used=1, vram_total=2)],
    )


def test_round_trip_preserves_every_nested_type():
    h = sample()
    back = Snapshot.loads(Snapshot(host=h).__json__()).host
    assert back.name == "box" and back.label == "Box" and back.self_host
    assert back.os == "Linux 6.9" and back.uptime == 1234.5
    assert back.cpu.pct == 42.5 and back.cpu.cores == [10.0, 90.0]
    assert back.cpu.load == [1.0, 0.5, 0.4]
    assert back.mem.total == 1000 and back.mem.pct == 75.0
    assert back.agents[0].kind == "claude" and back.agents[0].rss == 99
    assert back.sessions[0].msgs == 4
    assert back.ports["8080"].up is True and back.ports["8080"].label == "api"
    assert back.tmux[0].session == "fleet"
    assert back.gpus[0].name == "RTX"


def test_decoded_values_are_real_objects_not_dicts():
    """The bug this file exists to prevent."""
    h = Snapshot.loads(Snapshot(host=sample()).__json__()).host
    assert isinstance(h.cpu, CPU)
    assert isinstance(h.mem, Mem)
    assert isinstance(h.agents[0], Agent)
    assert isinstance(h.sessions[0], Session)
    assert isinstance(h.ports["8080"], Port)
    assert isinstance(h.tmux[0], TmuxPane)
    assert isinstance(h.gpus[0], GPU)


def test_host_with_a_cpu_field_is_not_decoded_as_a_cpu():
    """`Host.cpu` collides with the wire tag `cpu`; the tag has to win."""
    raw = json.dumps({"schema": SCHEMA, "collected": 1.0,
                      "host": {"_t": "host", "name": "x", "self_host": True,
                               "cpu": {"_t": "cpu", "pct": 5.0}}})
    h = Snapshot.loads(raw).host
    assert isinstance(h, Host) and isinstance(h.cpu, CPU)


def test_untagged_legacy_payload_still_decodes():
    """An older build's collector must not break a newer viewer's fleet."""
    raw = json.dumps({
        "schema": 2, "collected": 1.0,
        "host": {"name": "old", "self_host": False,
                 "cpu": {"pct": 9.0, "cores": [3.0]},
                 "agents": [{"pid": 4, "kind": "claude", "bound": True}],
                 "ports": {"80": {"up": True, "label": "web"}}}})
    h = Snapshot.loads(raw).host
    assert isinstance(h, Host) and h.name == "old"
    assert isinstance(h.cpu, CPU) and h.cpu.pct == 9.0
    assert isinstance(h.agents[0], Agent) and h.agents[0].pid == 4
    assert isinstance(h.ports["80"], Port) and h.ports["80"].label == "web"


def test_unknown_fields_are_ignored_not_fatal():
    raw = json.dumps({"schema": 99, "host": {"_t": "host", "name": "n",
                                             "future_field": [1, 2, 3]}})
    assert Snapshot.loads(raw).host.name == "n"


def test_empty_and_damaged_payloads_do_not_raise():
    """An unusable payload must degrade to a blank Host, not a bare dict.

    A ``dict`` here is the failure that hurts: the frame still renders, and then
    one attribute access on the far side of a panel raises for a reason that has
    nothing to do with the panel.
    """
    for raw in ('{"host": {}}', '{"host": null}', '{"host": {"_t": "host"}}',
                '{"host": {"name": "n", "cpu": {}}}'):
        host = Snapshot.loads(raw).host
        assert isinstance(host, Host), f"{raw} decoded to {type(host).__name__}"
        assert host.name in ("", "n", "host")
    for bad in ("", "not json", "[]"):
        try:
            Snapshot.loads(bad)
        except Exception:
            pass            # raising is acceptable for junk; hanging is not


def test_derived_percentages():
    assert Mem(total=0).pct == 0.0
    assert Mem(total=200, avail=50).pct == 75.0
    assert Mem(total=200, avail=50).used == 150
    assert GPU(vram_used=1, vram_total=4).vram_pct == 25.0