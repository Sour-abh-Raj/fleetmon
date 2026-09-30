"""Transport tests: the multi-device path.

Everything here is offline. The point is that the file transport -- the one
that works with no network at all -- carries a snapshot faithfully, and that a
viewer notices a peer that has gone quiet rather than rendering it as healthy.
"""

from __future__ import annotations

import json
import threading
import time

from fleetmon.config import Config, HostRef
from fleetmon.model import Host, Snapshot
from fleetmon.transport import (
    AutoTransport, FileTransport, _parse, build, watch_inbox,
)

from .helpers import fake_host


def test_file_round_trip(tmp_path):
    ref = HostRef(name="peer", via="file", target=str(tmp_path))
    tr = FileTransport()
    snap = Snapshot(host=fake_host("peer"))
    assert tr.push(ref, snap)
    back = tr.fetch(ref)
    assert back is not None and back.host.name == "peer"
    assert back.host.cpu.pct == snap.host.cpu.pct
    assert [a.pid for a in back.host.agents] == [a.pid for a in snap.host.agents]
    assert back.host.ports["8000"].label == "ollama"


def test_fetch_is_none_when_nothing_is_there(tmp_path):
    ref = HostRef(name="gone", via="file", target=str(tmp_path))
    assert FileTransport().fetch(ref) is None


def test_fetch_is_none_on_a_corrupt_file(tmp_path):
    (tmp_path / "x.json").write_text("{not json", encoding="utf-8")
    ref = HostRef(name="x", via="file", target=str(tmp_path))
    assert FileTransport().fetch(ref) is None


def test_a_reader_never_sees_a_half_written_file(tmp_path):
    """The rename is atomic; a torn read would read as "peer is broken"."""
    ref = HostRef(name="p", via="file", target=str(tmp_path))
    tr = FileTransport()
    tr.push(ref, Snapshot(host=fake_host("p")))
    tr.push(ref, Snapshot(host=fake_host("p", busy=True)))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, leftovers
    assert tr.fetch(ref).host.cpu.pct == 45.0


def test_transport_build_maps_every_name():
    for name in ("file", "ssh", "http", "auto", "nonexistent"):
        assert build(name, [HostRef(name="a")])["a"] is not None


def test_auto_transport_is_sticky(tmp_path, monkeypatch):
    """A peer that works must not silently fail over and change the picture."""
    ref = HostRef(name="p", via="auto", target=str(tmp_path))
    FileTransport().push(ref, Snapshot(host=fake_host("p")))
    auto = AutoTransport()

    calls: list[str] = []
    import fleetmon.transport as T

    real_ssh = T.SshTransport.fetch
    monkeypatch.setattr(T.SshTransport, "fetch",
                        lambda self, h: (calls.append("ssh"), real_ssh(self, h))[1])
    for _ in range(3):
        assert auto.fetch(ref) is not None
    # ssh is first in ORDER, so the very first probe legitimately tries it.
    # The property under test is that it is tried *once* -- a peer that answers
    # over the filesystem must not keep paying an ssh timeout every frame.
    assert calls == ["ssh"], calls
    assert isinstance(auto._chosen["p"], FileTransport)


def test_auto_gives_up_after_two_misses(tmp_path):
    ref = HostRef(name="p", via="auto", target=str(tmp_path))
    auto = AutoTransport()
    assert auto.fetch(ref) is None and auto.fetch(ref) is None
    assert "p" not in auto._chosen


def test_parse_survives_a_chatty_peer():
    """A peer that prints a warning before its payload must still parse."""
    raw = ('warning: could not read /var/run\n'
           '{"schema":3,"collected":1.0,"host":{"_t":"host","name":"p"}}\n')
    assert _parse(raw).host.name == "p"
    assert _parse('{"schema":3,"host":{"_t":"host","name":"p"}}').host.name == "p"
    assert _parse("") is None
    assert _parse("total garbage") is None


def test_inbox_watcher_delivers_once_per_change(tmp_path):
    got: list[Snapshot] = []
    ref = HostRef(name="p", via="file", target=str(tmp_path))
    FileTransport().push(ref, Snapshot(host=fake_host("p")))

    stop = watch_inbox(str(tmp_path), got.append)
    try:
        for _ in range(30):
            if got:
                break
            time.sleep(0.2)
    finally:
        stop.set()
    # The name comes from the payload, not the filename: the watcher is a
    # dumb pipe and the renaming to the configured peer name is `_poll`'s job.
    assert got and got[0].host.name == "p"
    assert len(got) == 1, "an unchanged file must not be re-delivered"


def test_publish_and_pull_make_a_two_host_fleet(tmp_path):
    """End to end, no network: one side publishes, the other side reads."""
    from fleetmon.app import Fleet

    outbox = tmp_path / "out"
    inbox = tmp_path / "in"
    outbox.mkdir()
    inbox.mkdir()

    writer = Fleet(Config(read_transcripts=False, collect=False,
                          peer_path=str(outbox), refresh=0.3))
    FileTransport().push(HostRef(name="writer", via="file", target=str(outbox)),
                         Snapshot(host=fake_host("writer", self_host=True)))

    ref = HostRef(name="writer", via="file", target=str(outbox))
    reader = Fleet(Config(read_transcripts=False, collect=False, hosts=[ref]))
    reader._put(Snapshot(host=fake_host("here", self_host=True)).host)
    reader._poll(ref, FileTransport())

    names = {h.name for h in reader.view()}
    assert names == {"here", "writer"}, names
    writer.shutdown()
    reader.shutdown()


def test_a_peer_that_never_answers_becomes_an_error_not_a_silent_gap(tmp_path):
    """Staleness is the single most important thing a fleet view can report."""
    from fleetmon.app import Fleet

    ref = HostRef(name="ghost", via="file", target=str(tmp_path))
    f = Fleet(Config(read_transcripts=False, collect=False, hosts=[ref]))
    for _ in range(3):
        f._poll(ref, FileTransport())
    assert "ghost" in f.errors
    assert f.view() == []
    f.shutdown()