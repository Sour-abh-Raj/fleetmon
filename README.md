# fleetmon

A live, multi-device view of the agents running on your machines — as a
terminal dashboard, not a web page.

```
┌─ ATTENTION───────────────────────────────────────┐ ┌─ AGENTS                                    6──┐
│ ● nixos  disk 91%                                │ │  PID  KIND      MODEL    PROJECT   WORKING ON      │
│ ● nixos  :8000 ollama down                      │ │ 18400 claude   sonnet   quant     backtest sweep   │
└──────────────────────────────────────────────────┘ │ 21124 mcp      -       research   reading papers  │
┌─ SYSTEM                               2 hosts───┐ │ 18348 claude   opus     ai-research writing tests   │
│ cpu  18.8% ░░░░██░█░░░░░░░░ L0.0                 │ └─────────────────────────────────────────────────────┘
│ mem  72.8% ████████████████████        12.2G/16 │
│ dsk  85.3% ████████████████████████   435.1G/5  │ ┌─ READING─────────────────────────────────────────┐
└──────────────────────────────────────────────────┘ │ gnosis cpu ▄▄▄▄░░▄  mem ▄▄░░░▄▄   1.2M/s          │
┌─ LLM SERVER���─────────────────────────────────────┐ └─────────────────────────────────────────────────────┘
│ ● gnosis :11434  ollama     14B        12ms      │
└──────────────────────────────────────────────────┘
```

- **Zero runtime dependencies.** Python 3.9+, stdlib only. `psutil` is used
  when present and every collector has a fallback when it is not.
- **Windows, macOS and Linux**, on the same code path.
- **Resizes with the window**, including inside a tmux split pane, because the
  geometry is re-read every frame rather than caught from an event.
- **Its own scrollback**, so `j`/`k` scroll back through real frames on every
  platform instead of relying on the terminal's own buffer.
- **A fleet**, not one machine: ssh, shared directory or HTTP, mixed freely.

---

## Install

```sh
pip install fleetmon            # or: uv tool install fleetmon
pip install "fleetmon[fast]"    # adds psutil for richer process/CPU data
```

Then, anywhere:

```sh
fleetmon
```

There is nothing to configure for the common case. The first frame shows this
machine's vitals, its agent processes, its inference servers and its tmux
panes, and it keeps up as you resize.

## Keys

| | |
|---|---|
| `j` `k` / `↓` `↑` | scroll one frame |
| `PgDn` `PgUp` | scroll a page |
| `g` / `Home` | oldest retained frame |
| `G` / `End` | back to live |
| `p` / space | pause the refresh (the frame stays, the data freezes) |
| `r` | reset trend history and the frame buffer |
| `w` | force the narrow layout |
| `Tab` | rotate which panel wins the space in a small window |
| `?` | help |
| `q` / `Ctrl-C` | quit |

Scrollback is the dashboard's own ring buffer, not the terminal's. That is
deliberate: the alternate screen has no history at all, and a view written
inline accumulates megabytes an hour.

## What it shows

| Panel | What it answers |
|---|---|
| **ATTENTION** | Does anything want a human right now? Empty means empty. |
| **AGENTS** | Which agentic processes are running, doing what, and since when. |
| **SYSTEM** | CPU (per core), memory, disk, network, GPU — per host. |
| **LLM SERVERS** | Which inference endpoints are up, how fast, what they serve. |
| **TMUX** | What is running in each pane, and the command to reach it. |
| **READING** | Short trend lines, so "boring" is distinguishable from "wedged". |
| **SESSIONS** | Transcript-level sessions, including ones with no live process. |

### Agent detection

Built-in signatures cover Claude Code, `claude-mcp`, `idea-loop`, the paper
humanizer and their `npx`/`uv` wrappers, while skipping Electron helper
processes and the Claude Desktop app shell — a window is not an agent. Add
your own in one line:

```toml
[detect]
agent_patterns = [["scraper", "scrapy"], ["paper-run", "regen_paper\\.py"]]
ignore_cmd = ["grep", "vim"]
```

## A fleet

Every machine publishes a snapshot; every viewer pulls them. Nothing is
pushed unsolicited and no port is opened by default.

### Over ssh (most people)

On the box you want to watch:

```sh
fleetmon --serve --port 8731     # or: just run fleetmon there
```

On the box you watch from:

```toml
[[hosts]]
name = "nixos"
via  = "ssh"
target = "tunix"
```

```sh
fleetmon --peer tunix
```

### Over a shared directory (no network at all)

Works across a Dropbox, a NAS or a synced git repo. Each machine writes into
the folder it can see; each reads the others.

```toml
# on gnosis
peer_path = "~/fleetmon-inbox"

[[hosts]]
name  = "nixos"
via   = "file"
target = "~/fleetmon-inbox"
```

### Over HTTP

`fleetmon --serve` on one machine, `via = "http"` elsewhere. Loopback-only by
default; `--host 0.0.0.0` is opt-in and should stay behind a tunnel.

```sh
fleetmon --serve --port 8731 --host 127.0.0.1
```

### `auto`

`via = "auto"` tries ssh, then a shared file, then HTTP, and **sticks with
whatever answered**. A peer that flickers between two mechanisms shows you two
different truths; this removes that failure mode.

A peer that stops answering does not disappear from the dashboard. It goes
grey, then red, then into ATTENTION as `silent 47s` — because a monitor that
renders a dead machine as healthy is worse than no monitor.

## Non-interactive use

| | |
|---|---|
| `fleetmon --once` | render one frame and exit (what `tmux capture-pane` wants) |
| `fleetmon --collect --raw` | emit this machine's snapshot as JSON |
| `fleetmon --serve` | run the HTTP snapshot endpoint |
| `fleetmon --check` | print what fleetmon can see about this machine |
| `fleetmon --write-inbox DIR` | collect and publish into a shared directory |
| `fleetmon --init-config [PATH]` | write a commented example config |

As a library:

```python
from fleetmon import Config, collect, render_once

snap = collect(Config())          # a Snapshot: host, agents, ports, sessions
print(snap.host.cpu.pct, len(snap.host.agents))
frame = render_once(w=100, h=30)  # one frame as a string, no terminal needed
```

## Configuration

Layered, lowest to highest:

1. built-in defaults
2. `./fleetmon.toml`, then `~/.config/fleetmon/fleetmon.toml`
3. `$FLEETMON_CONFIG`
4. `FLEETMON_*` environment variables
5. command-line flags

```sh
fleetmon --init-config ~/.config/fleetmon/fleetmon.toml
```

Frequently useful:

```toml
[ui]
label      = "gnosis"     # shown instead of the hostname
refresh    = 1.0
history    = 400          # frames kept for scrollback
wide_at    = 132          # collapse to one column below this width
alt_screen = true         # false keeps the terminal's own scrollback too

[collect]
read_transcripts = true    # parse ~/.claude/projects/*.jsonl
probe_ports      = true

[limits]
warn_cpu = 78.0
crit_cpu = 92.0
stale_host = 8.0           # seconds before a silent peer is called stale
```

Environment: `FLEETMON_LABEL`, `FLEETMON_REFRESH`, `FLEETMON_HISTORY`,
`FLEETMON_CONFIG`, `FLEETMON_NO_ALT`.

## Why it looks the way it does

**Layout is a priority ladder, not a grid.** Each panel declares the width and
height it needs to be legible and how much of the current data it would like.
Space is assigned from the most important panel down; a panel that cannot be
given its minimum is dropped with a name in the footer rather than rendered as
a broken sliver. Below `wide_at` the columns collapse into one, in priority
order, with ATTENTION first — because the one thing that must survive every
resize is "does anything need me".

**Geometry is polled, not trapped.** `SIGWINCH` does not exist on Windows
(where the equivalent is `SetConsoleCtrlHandler`) and tmux split panes can
change a child's size without signalling it at all. `shutil.get_terminal_size()`
costs microseconds against a frame budget of tens of milliseconds and is
correct on all three platforms, including after a suspend.

**The glyph set is conservative on purpose.** Anything richer than full block,
lower half block, light shade and single-line box drawing is missing from at
least one of Consolas, Lucida Console and Cascadia Mono, and a console renders
a missing glyph as a blank with no error at all. Magnitude is carried by
colour, not by which sub-character block is used. See [docs/glyphs.md](docs/glyphs.md).

**Escalation is attention, not noise.** Non-problems — an empty todo list, a
port that was never meant to serve on this host — are not allowed into
ATTENTION. A panel that cries wolf is a panel nobody reads.

## Extending it

A collector is a callable that returns anything JSON-serialisable:

```python
from fleetmon.collectors import register

@register("queue-depth")
def queue_depth(ctx):
    return {"rabbitmq": 8123}
```

Its result lands in `host.extra["queue-depth"]` and is available to custom
panels and to `--collect --raw`. Every collector is isolated: one that raises
costs its own row, never the frame.

## Development

```sh
pip install -e ".[fast,dev]"
pytest -q
```

## Licence

MIT. See [LICENSE](LICENSE).