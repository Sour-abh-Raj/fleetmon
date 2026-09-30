"""fleetmon -- a live, multi-device view of the agents running on a fleet.

    from fleetmon import collect, render

Stdlib only. Works on Windows, macOS and Linux, with or without ``psutil``.

The public surface is deliberately small:

``collect(cfg)``  take a snapshot of this machine
``Fleet``         the background aggregator that keeps host state fresh
``App``           the live TUI
``render_once``   one frame as a string, for tests and pipes
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "collect", "Config", "render_once", "main"]


def __getattr__(name):
    """Import lazily so ``import fleetmon`` stays cheap.

    A dashboard is often imported by something else -- a status endpoint, a
    test, a plugin -- and paying for the whole render stack to read a version
    number is the kind of cost that makes people vendor the file instead.
    """
    if name == "collect":
        from .collectors import collect
        return collect
    if name == "Config":
        from .config import Config
        return Config
    if name == "render_once":
        return _render_once
    if name == "main":
        from .cli import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _render_once(cfg=None, w: int = 120, h: int = 40, *, colors: bool = True,
                 hosts=None):
    """Render a single frame and return it as a string.

    The seam that makes this testable: no terminal, no threads, no timing. It
    delegates to :meth:`App.build_frame` rather than assembling its own frame,
    because a second implementation of "the frame" is a second thing to keep
    correct -- and the header and footer are exactly the part that goes missing
    first, leaving a render that looks plausible and is not what ships.

    ``hosts`` renders a frame for hosts you already have instead of collecting
    this machine. On Windows a real collection costs several seconds of WMI
    process enumeration, which is a poor thing to ask of a layout test.
    """
    from .app import App, Fleet
    from .collectors import collect
    from .console import decolor

    cfg = cfg or Config
    fleet = Fleet(cfg)
    if hosts is None:
        fleet._put(collect(cfg).host)
    else:
        for host in hosts:
            fleet._put(host)
    app = App(cfg)
    app.fleet = fleet
    try:
        frame = app.build_frame(size=(w, h))
    finally:
        fleet.shutdown()
    # No colours when the caller is a pipe: `tmux capture-pane -p` and CI logs
    # both want the text, and a frame nobody asked to colour should not arrive
    # wrapped in escape sequences.
    return frame if colors else decolor(frame)
