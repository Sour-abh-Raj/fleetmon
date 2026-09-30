"""Agent-detection tests.

Detection is the part of fleetmon most likely to be quietly wrong: a machine
with three agents running and a detector that finds none renders a perfectly
healthy dashboard, and nothing anywhere reports an error. Every case here is a
real command line seen on a real machine.
"""

from __future__ import annotations

import pytest

from fleetmon.collectors.processes import (
    ELECTRON_RE, build_patterns, classify, extract_model,
)

#: Quoted, space-containing absolute path -- the shape Windows produces for any
#: launcher invoked from the Start Menu or a shortcut.
QUOTED_WIN = (r'"C:\Users\soura\AppData\Local\Microsoft\WinGet\Links'
              r'\claude.exe"')
#: The npm wrapper under a Program Files node, which is how the CLI runs on a
#: stock Windows install. Every path is quoted, so the pattern has to survive
#: a quote on *both* sides of the executable it names.
QUOTED_NPX = (r'"C:\Program Files\nodejs\node.exe" '
              r'"C:\Users\soura\AppData\Roaming\npm\node_modules'
              r'\@anthropic-ai\claude-code\cli.js"')


def C(cmd: str):
    return classify(cmd, build_patterns())


@pytest.mark.parametrize("cmd", [
    QUOTED_WIN,
    QUOTED_NPX,
    r"C:\Users\soura\bin\claude.exe",
    "/home/soura/.local/bin/claude",
    "claude",
    "claude --model claude-opus-4-5",
    "npx @anthropic-ai/claude-code",
    r"node C:\npm-cache\_npx\abc\node_modules\@anthropic-ai\claude-code\cli.js",
    r"uv run --with anthropic claude",
])
def test_claude_is_detected_however_it_was_launched(cmd):
    """The regression: ``($|\\s)`` cannot match a path ending in a quote.

    Every quoted form below ends in ``"``, so the old terminator rejected all of
    them and the no-psutil path found zero agents on a machine running three.
    """
    assert C(cmd) == "claude", cmd


@pytest.mark.parametrize("cmd,label", [
    ("ollama serve", "llm-server"),
    ("vllm.entrypoints.openai.api_server", "llm-server"),
    ("llama-server -m qwen", "llm-server"),
    ("npx @openai/codex", "codex"),
    (r'"C:\tools\aider.exe"', "aider"),
    ("python -m mcp_server_git", "mcp"),
    ("jupyter-lab", "notebook"),
])
def test_other_signatures(cmd, label):
    assert C(cmd) == label, cmd


@pytest.mark.parametrize("cmd", [
    "notepad.exe",
    "explorer.exe",
    "clauded",                     # a prefix must not match
    "my-claudette",                # nor an infix
    r"C:\Users\soura\AppData\Local\Temp\claude\claude-pwd-ps-d441",
    r"powershell.exe -c 'cd C:\Users\soura\.claude\projects'",
])
def test_non_agents_are_not_agents(cmd):
    assert C(cmd) is None, cmd


def test_a_directory_named_claude_is_not_an_executable():
    """The terminator accepts a quote and whitespace but not a backslash.

    Every shell this tool runs in has `AppData\\Local\\Temp\\claude\\` as its
    working directory, so a backslash terminator made fleetmon report its own
    harness shells as agents -- one per tool call, growing as you work.
    """
    shell = (r'"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" '
             r"-NoProfile -Command \"Set-Location "
             r"'C:\Users\soura\AppData\Local\Temp\claude\claude-pwd-ps-1'; "
             r'git status"')
    assert C(shell) is None
    # ...while the executable in that same directory is still detected.
    assert C(r'"C:\Users\soura\AppData\Local\Temp\claude\claude.exe"') == "claude"


def test_the_claude_desktop_app_is_not_an_agent():
    """On Windows the app ships `claude.exe` under WindowsApps, and every one
    of its helpers is a browser process. Counting those produces a laptop with
    twelve "agents" that are Chrome."""
    app = (r'"C:\Program Files\WindowsApps\Claude_1.0.0_x64\claude.exe" '
           r'--type=renderer')
    assert C(app) is None
    assert ELECTRON_RE.search(app)


@pytest.mark.parametrize("cmd", [
    r'"C:\Program Files\Git\bin\bash.exe" -c "source snapshot && claude"',
    "/bin/bash -c claude",
    r"pwsh -NoProfile -Command claude",
])
def test_a_shell_is_recognised_as_a_shell(cmd):
    """``SHELL_RE`` is searched, not matched.

    A shell whose path contains a space arrives quoted, so its name is preceded
    by a separator rather than by the start of the string. Anchoring it meant
    ``bash -c claude`` was never recognised as a shell, and the parent shell of
    an agent was counted as an agent.
    """
    from fleetmon.collectors.processes import SHELL_RE

    assert SHELL_RE.search(cmd), cmd


def test_a_real_shell_command_line_is_a_shell_not_a_random_name():
    """The separator requirement is what keeps this from matching every
    command -- ``envelope`` contains ``env``, ``bashful`` contains ``bash``."""
    from fleetmon.collectors.processes import SHELL_RE

    assert SHELL_RE.search("envelope --to claude") is None
    assert SHELL_RE.search("bashful-thinking --run") is None


def test_the_model_is_shortened_to_its_family():
    assert extract_model("claude --model claude-opus-4-5") == "claude-opus"
    assert extract_model("claude --model claude-sonnet-4-6") == "claude-sonnet"
    assert extract_model("vllm --model mistralai/Mistral-7B") == "Mistral-7B"
    assert extract_model("claude") == ""


def test_user_patterns_extend_the_defaults():
    pats = build_patterns([["scraper", "scrapy"]])
    assert classify("scrapy crawl quotes", pats) == "scraper"
    assert classify("claude", pats) == "claude", "defaults must survive"


def test_a_broken_user_pattern_does_not_take_the_table_down():
    pats = build_patterns([["bad", "([unclosed"]])
    assert classify("claude", pats) == "claude"
