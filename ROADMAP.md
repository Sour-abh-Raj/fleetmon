# Fleetmon — state of the project, and what to build next

Written after the first month of real use: the monitor now runs on two machines
and has been used to catch things by hand. This records what it is, what is
known to be wrong with it, and what would make it worth more than the effort it
currently costs to keep.

## What it is

A live view of every agent, inference server and session on a fleet of
machines. One terminal, both boxes, no configuration required.

The premise that survived contact with daily use: **the first screen should be
triage.** If an alarm has to compete with a dozen gauges for attention, it is
not doing its job. So ATTENTION comes first and names a cause and a
consequence; gauges come after.

## The personal monitor vs the module

They are deliberately two things, and the distinction has held up:

- **`agentmon`** (`~/bin/fleetmon`, `Documents\Projects\agentmon-py`) is the
  one actually used. One file, dense, opinionated. It is the place a new idea
  gets tried.
- **`fleetmon`** (this repo, `Documents\Projects\fleetmon-py`) is the
  publishable module: same ideas, generalised, configurable, installable.

Every fix lands in the personal monitor first and is ported here with a test
attached. That ordering is the whole reason the personal one is trustworthy —
the module is downstream of something that demonstrably works.

## What is known to be wrong

### 1. Prompt text is rendered, and prompt text contains secrets — HIGH

The `WORKING ON` column shows the argument a user typed. Users paste
credentials into prompts. The first time this monitor was captured on the NixOS
box, a live password was sitting in that column — and the same snapshot is what
`--push` ships to the peer, so it also lands on disk and crosses the network.

This is the single most serious thing about the product right now, and it is a
design problem rather than a bug: the column is useful precisely because it
shows what the user typed.

Fix: redact at the point of ingestion, before anything is stored, rendered or
pushed. Patterns worth covering: `password`/`passwd`/`pwd` followed by a
delimiter, `sk-…`, `ghp_…`, `AKIA…`, `-----BEGIN … PRIVATE KEY-----`, and
`sudo` prompts. Redaction must happen in the collector, not in the panel, so no
transport or export path can leak the original. Add a test that asserts a known
fake secret never appears in a rendered frame, a snapshot, or a pushed file.

### 2. The zero-dependency path shipped broken

With no `psutil`, the detector reported **zero agents** on a machine running
three. Windows quotes any path containing a space, and the word terminator was
`($|\s)`, which cannot match a closing quote. The headline claim — install it,
it works — failed exactly where it was first tested.

It shipped because that path was only ever exercised on the machine that had
`psutil`.

Fix: a CI matrix over Windows/Linux/macOS **and** with `psutil` uninstalled,
asserting the agent count is the same either way. This is the process failure
underneath the bug, and it is more valuable than the regex fix.

### 3. Input handling was never tested, because it cannot fail loudly

On Windows, `isatty` was computed only when *not* on Windows and then tested on
both, so it was permanently false and no key did anything. Not scrolling —
*nothing*. The program looked perfect and responded to nothing.

The general lesson: **a monitor's failure mode is silence, so its tests have to
assert on behaviour, not on return values.** Anything that is only observable
by pressing a key needs a test that presses it — a fake console for the Windows
scan-code path, a scripted escape sequence for the POSIX one.

## What to build next

Ordered by value per unit of effort. The first three are cheap and make
everything after them possible.

### Tier 1 — do these first

**Cost and token visibility.** Every transcript already has token counts that
are parsed and then thrown away. "Spend today: $4.20, 62% on Opus" is the
number that changes behaviour, and it is the thing that makes this a fleet
*management* tool rather than a fleet *process* tool. Half the data is already
there.

**Secret redaction.** Above. Non-negotiable before this is installed anywhere
else.

**A frame exporter.** Press a key, write the current frame to a file or the
clipboard. Right now the answer to "what was the screen when it broke?" is a
screenshot, which is not available over ssh and does not survive a tmux resize.
The frame buffer already exists; this is a few lines.

### Tier 2 — the ones that would actually be used

**Sticky, acknowledged alerts.** An alert should persist until someone acts on
it, and be dismissible with a key. Right now a peer that stops answering is
announced every second and then scrolls away, which trains you to ignore the
panel that exists to stop you ignoring things.

**Per-agent drill-down.** Select an agent, see its recent tool calls, tokens and
last prompt. The AGENTS table is a summary of something you often want to
actually look at.

**Adaptive refresh.** Idle at 4s, drop to 1s when something changes. The sweep
work above made a tick cheap enough for this to be worth it.

**A "what changed" diff row.** Fleet monitors are read by comparing two moments.
Showing only the delta since the last frame — one agent died, one server went
warm, memory moved 4G — is more information per row than another gauge.

### Tier 3 — tempting, expensive, or dangerous

**Writing to agents.** Sending a prompt to a remote agent from the monitor. It
is the obvious endpoint of "two-way visibility" and it is a remote-control
interface to a machine that is running code. Deliberately out of scope until
the read-only side is genuinely solid, and then only behind an explicit flag
that says so on screen.

**Custom panels from config.** The `@register` hook exists; letting a user point
at a Python file from a TOML file is a natural next step and also an obvious
remote-code-execution surface.

**Sound on critical.** Cheap, and people turn it off within a day. Only worth it
with a strict per-alert opt-in.

## Engineering practices this project has actually learned

1. **Assert on rendered output, not on return values.** Every layout claim in
   the test suite is "exactly *h* lines, none wider than *w*". A frame that is
   one line short passes every other test.
2. **Every detector case is a real command line from a real machine.** An
   invented test case would not have caught the quoted-path bug, because nobody
   invents that shape — Windows does.
3. **Test the path that ships by default, not the path that is convenient.**
   The zero-dependency claim is the product's headline and it was the least
   tested code in it.
4. **Profile before optimising, and question the first measurement.** The scan
   cost 1.3 s; the obvious suspect (command-line reads) turned out to be
   `create_time` and `memory_info`, and caching the wrong one would have
   changed nothing. `tasklist` was measured too and was no better.
5. **A monitor that burns a core is competing with the agents it watches.** Any
   collector whose cost approaches the tick interval is a design failure, not a
   tuning problem.

## Where it runs

- Windows 11, `~/bin/fleetmon`, in a resizable console window.
- NixOS, `~/bin/agentmon`, in tmux session `agentmon`.
- Peers exchange JSON snapshots; `ssh`, `file` and `http` transports, with
  `auto` failing over in a fixed order.