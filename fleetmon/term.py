"""Terminal control: size discovery, raw-mode input, and the scrollback pager.

Three problems this solves, all of which bite hard on a live dashboard:

**Resize.** The layout has to follow the window. ``SIGWINCH`` exists on POSIX
but not on Windows, where the console reports a resize through
``SetConsoleCtrlHandler`` instead -- so the portable answer is simply to ask
the terminal for its size every frame. It is a cheap syscall and it is
correct everywhere, which beats a fast path that is wrong on one platform.

**Scrollback.** A full-screen live view written to the alternate screen buffer
has *no* scrollback: you cannot scroll up past the current frame, because
there is no history, only the one buffer being overwritten. And a view that
instead writes inline accumulates every one of its frames into real scrollback,
which at one frame per second is megabytes an hour.

So the dashboard keeps its own ring buffer of recent frames and renders a
pager over it. That is cheaper than real scrollback, it is searchable, and it
behaves identically on all three platforms.

**Input.** Keys must be read without blocking the render loop, and without
the terminal's own line discipline echoing them into the display.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

IS_WIN = os.name == "nt"


# --- keys --------------------------------------------------------------------

@dataclass(frozen=True)
class Key:
    name: str
    char: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Key({self.name!r})"


ESC = Key("escape")
ENTER = Key("enter")
BACKSPACE = Key("backspace")
UP = Key("up")
DOWN = Key("down")
LEFT = Key("left")
RIGHT = Key("right")
HOME = Key("home")
END = Key("end")
PGUP = Key("pageup")
PGDN = Key("pagedown")
RESIZE = Key("resize")

_CTRL_NAMES = {
    "a": "home", "b": "left", "d": "right", "e": "end", "f": "right",
    "h": "left", "k": "up", "n": "down", "p": "up",
    "u": "pageup", "d": "pagedown", "k": "up",
}

# Function keys arrive as ESC [ <letter> ~ or ESC O <letter>
_SS3 = {"A": UP, "B": DOWN, "C": RIGHT, "D": LEFT, "H": HOME, "F": END}
_CSI_TILDE = {
    "1": HOME, "2": Key("insert"), "3": Key("delete"), "4": END,
    "5": PGDN, "6": PGUP, "7": HOME, "8": END,
}

#: How many frames back a reader may be and still be carried along by a new
#: frame arriving. See :meth:`Pager.push`.
FOLLOW = 2


def translate(seq: str) -> Key | None:
    """Map a raw escape sequence to a :class:`Key`.

    Returns ``None`` for sequences we deliberately ignore (mouse reports,
    bracketed paste, focus events) rather than guessing a binding for them.
    """
    if not seq:
        return None
    if seq == "\t":
        return Key("tab")
    if seq in ("\x1b",):
        return ESC
    if seq in ("\r", "\n"):
        return ENTER
    if seq in ("\x7f", "\b"):
        return BACKSPACE
    if seq == "\x03":
        return Key("ctrl-c")
    if seq[0] != "\x1b":
        return Key("char", seq)

    body = seq[2:] if seq[1:2] in ("[", "O") else seq[1:]
    if not body:
        return ESC
    if body.startswith("<"):
        return None                     # SGR mouse / focus report
    # Bracketed paste arrives as a whole run of text, not a single keypress.
    # Returning ESC for it would quit the dashboard on the first paste.
    if body.startswith(("200~", "201~")):
        return None
    # A CSI sequence is ``ESC [ params ~`` -- the tilde *terminates* it, it
    # never leads. Testing the first character instead of the last means
    # PgUp/PgDn arrive as ESC and scroll one line rather than one page.
    if body.endswith("~"):
        return _CSI_TILDE.get(body[:-1].split(";")[0])
    head = body[0]
    if head in _SS3:
        return _SS3[head]
    if head in ("M", "m"):
        return None                     # legacy mouse press / release
    return ESC


# --- terminal ----------------------------------------------------------------

class Terminal:
    """Owns raw mode, the alternate screen, and the key queue."""

    def __init__(self, *, alt_screen: bool = True, mouse: bool = False,
                 size: tuple[int, int] | None = None):
        self.out = sys.stdout
        self.keys: deque[Key] = deque(maxlen=64)
        self.alt_screen = alt_screen
        self.mouse = mouse
        #: A fixed size, for rendering into something that is not a terminal.
        #: ``None`` means "ask the terminal", which is the live path.
        self.fixed_size = size
        self._raw = False
        self._saved = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._entered = False

    # -- geometry ------------------------------------------------------------

    def size(self) -> tuple[int, int]:
        """Current ``(cols, rows)``, re-read every call.

        Polling beats a resize handler here: the handler would be one code path
        on POSIX and a different one on Windows, and both can silently miss an
        event. Asking is a handful of microseconds against a frame budget that
        is tens of milliseconds.
        """
        if self.fixed_size:
            return max(20, self.fixed_size[0]), max(8, self.fixed_size[1])
        try:
            sz = shutil.get_terminal_size(fallback=(100, 30))
            return max(20, sz.columns), max(8, sz.lines)
        except Exception:
            return 100, 30

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "Terminal":
        self.enter()
        return self

    def __exit__(self, *exc) -> None:
        self.exit()

    def enter(self) -> None:
        if self._entered:
            return
        self._entered = True
        try:
            if not self.out.isatty():
                return
        except Exception:
            return
        if self.alt_screen:
            self.out.write("\x1b[?1049h\x1b[H\x1b[2J")
        if self.mouse:
            self.out.write("\x1b[?1000h\x1b[?1006h")
        self.out.write("\x1b[?25l")          # hide cursor
        self.out.flush()
        self._raw = self._enter_raw()
        self._start_reader()

    def exit(self) -> None:
        if not self._entered:
            return
        self._entered = False
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=0.4)
        self._exit_raw()
        try:
            if self.mouse:
                self.out.write("\x1b[?1006l\x1b[?1000l")
            self.out.write("\x1b[?25h")      # show cursor
            if self.alt_screen:
                self.out.write("\x1b[?1049l")
            self.out.flush()
        except Exception:
            pass

    # -- raw mode ------------------------------------------------------------

    def _enter_raw(self) -> bool:
        if IS_WIN:
            try:
                import msvcrt
                import ctypes
                # Disable line input and echo on the input handle only; the
                # output handle must keep processing VT.
                k = ctypes.WinDLL("kernel32", use_last_error=True)
                k.GetConsoleMode.restype = ctypes.c_int
                k.GetConsoleMode.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_ulong)]
                k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                h = ctypes.c_void_p(k.GetStdHandle(-10))
                mode = ctypes.c_ulong()
                if k.GetConsoleMode(h, ctypes.byref(mode)):
                    self._saved = mode.value
                    k.SetConsoleMode(h, (mode.value & ~0x0002) | 0x0100)
                del msvcrt
                return True
            except Exception:
                return False
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            return True
        except Exception:
            return False

    def _exit_raw(self) -> None:
        if not self._raw:
            return
        self._raw = False
        if IS_WIN:
            try:
                import ctypes
                k = ctypes.WinDLL("kernel32", use_last_error=True)
                k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                k.GetConsoleMode.restype = ctypes.c_int
                k.GetConsoleMode.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_ulong)]
                if self._saved is not None:
                    k.SetConsoleMode(ctypes.c_void_p(k.GetStdHandle(-10)),
                                     ctypes.c_ulong(self._saved))
            except Exception:
                pass
            return
        try:
            import termios
            if self._saved is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
        except Exception:
            pass

    # -- input ---------------------------------------------------------------

    def _start_reader(self) -> None:
        def pump() -> None:
            if IS_WIN:
                self._pump_windows()
            else:
                self._pump_posix()

        self._reader = threading.Thread(target=pump, name="fleetmon-keys", daemon=True)
        self._reader.start()

    def _pump_windows(self) -> None:
        import msvcrt
        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue
            seq = ""
            # getwch returns one char; an escape sequence arrives one at a
            # time, so keep consuming while input is still buffered.
            while msvcrt.kbhit():
                seq += msvcrt.getwch()
                if len(seq) > 12:
                    break
            k = translate(seq)
            if k:
                self.keys.append(k)

    def _pump_posix(self) -> None:
        import select
        try:
            fd = sys.stdin.fileno()
        except Exception:
            return
        buf = ""
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
            except Exception:
                return
            if not r:
                continue
            try:
                ch = os.read(fd, 64).decode("utf-8", "replace")
            except Exception:
                return
            if not ch:
                continue
            buf += ch
            while buf:
                if buf[0] != "\x1b":
                    k, buf = translate(buf[0]), buf[1:]
                    if k:
                        self.keys.append(k)
                    continue
                # An escape might be a bare ESC key or the start of a
                # sequence. A lone ESC is only a keypress once input has gone
                # quiet, otherwise it swallows the sequence behind it.
                m = _seq_len(buf)
                if m is None:
                    if self._flush_escape():
                        continue
                    break
                seq, buf = buf[:m], buf[m:]
                k = translate(seq)
                if k:
                    self.keys.append(k)

    def _flush_escape(self) -> bool:
        time.sleep(0.02)
        return True

    def poll(self) -> list[Key]:
        keys = list(self.keys)
        self.keys.clear()
        return keys

    # -- drawing -------------------------------------------------------------

    def draw(self, frame: str, height: int) -> None:
        """Blit a frame without letting it scroll the terminal.

        Writing to the last line and then scrolling would advance the
        viewport every frame, so the frame is padded to exactly one screen
        and written with the cursor pinned home first.
        """
        try:
            self.out.write("\x1b[H")
            lines = frame.split("\n")
            for i in range(height):
                self.out.write("\x1b[K")
                if i < len(lines):
                    self.out.write(lines[i])
                if i < height - 1:
                    self.out.write("\r\n")
            self.out.write("\x1b[J")
            self.out.flush()
        except (KeyboardInterrupt, BrokenPipeError):
            raise
        except Exception:
            pass

    def write_line(self, text: str) -> None:
        """Print one line outside the alternate screen (help, errors)."""
        try:
            if self.alt_screen:
                self.out.write("\x1b[?1049l")
            self.out.write(text.rstrip("\n") + "\n")
            self.out.flush()
            if self.alt_screen:
                self.out.write("\x1b[?1049h\x1b[H\x1b[2J")
                self.out.flush()
        except Exception:
            pass


def _seq_len(buf: str) -> int | None:
    """Length of the escape sequence at the head of ``buf``, if complete."""
    if len(buf) < 2:
        return None
    if buf[1] not in "[O":
        return 1
    i = 2
    while i < len(buf):
        ch = buf[i]
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ch == "~":
            return i + 1
        i += 1
    return None


# --- pager -------------------------------------------------------------------

@dataclass
class Pager:
    """Scrollback over recently rendered frames.

    ``pos == 0`` is *live*: the newest frame, updating. Any other position is
    a frozen historical frame. That distinction is why ``clamp`` returns to 0
    rather than to the newest index -- scrolling off the end should resume the
    live view, not park you on a stale frame that happens to be the last one.
    """

    history: deque = field(default_factory=lambda: deque(maxlen=600))
    pos: int = 0
    limit: int = 200

    def push(self, frame: str) -> None:
        self.history.append(frame)
        if self.pos:
            # Follow the tail only when the reader is a frame or two behind it.
            # The window has to be absolute: scaled to the history, a reader who
            # pressed `g` to jump to the oldest frame looks "near the tail" in a
            # six-frame buffer and gets dragged forward, which is the opposite of
            # what they asked for. A very short history is exempt because there
            # is nothing to read in it anyway.
            if self.pos <= FOLLOW or len(self.history) <= FOLLOW + 2:
                self.pos = min(self.pos + 1, len(self.history) - 1)

    @property
    def live(self) -> bool:
        return self.pos == 0

    @property
    def depth(self) -> int:
        return len(self.history)

    def view(self) -> str | None:
        """The frame to draw: live, or the historical one under the cursor."""
        if not self.history:
            return None
        if self.live:
            return self.history[-1]
        idx = max(0, len(self.history) - 1 - self.pos)
        return self.history[idx]

    def back(self, n: int = 1) -> None:
        self.pos = min(self.pos + n, max(0, len(self.history) - 1))

    def forward(self, n: int = 1) -> None:
        self.pos = max(0, self.pos - n)

    def top(self) -> None:
        self.pos = max(0, len(self.history) - 1)

    def bottom(self) -> None:
        self.pos = 0

    def clamp(self) -> None:
        self.pos = max(0, min(self.pos, max(0, len(self.history) - 1)))

    def handle(self, key: Key, page: int) -> bool:
        """Apply a navigation key. Returns False when it is not navigation."""
        n = key.name
        if n in ("up",) or (n == "char" and key.char in ("k",)):
            self.back()
        elif n == "down" or (n == "char" and key.char in ("j",)):
            if self.live:
                return True
            self.forward()
        elif n == "pageup" or (n == "char" and key.char in ("\x04",)):
            self.back(page)
        elif n == "pagedown" or (n == "char" and key.char in ("\x15",)):
            self.forward(page)
        elif n == "home" or (n == "char" and key.char == "g"):
            self.top()
        elif n == "end" or (n == "char" and key.char == "G"):
            self.bottom()
        else:
            return False
        self.clamp()
        return True

    def scrollbar(self, height: int) -> str:
        """A one-column position indicator for the frame stack."""
        total = len(self.history)
        if total <= 1 or height < 3:
            return ""
        win = min(height, total)
        if self.live:
            start = total - win
        else:
            idx = max(0, total - 1 - self.pos)
            start = min(max(0, idx - win // 2), total - win)
        cells = []
        for i in range(height):
            # ":" rather than a light-vertical rule: box drawing gets thin and
            # inconsistent at terminal row heights below about 12px, and this
            # column has to be legible in a quarter-screen pane.
            cells.append("│" if start <= i < start + win else ":")
        return "".join(cells)
