"""Emit OSC escape sequences to set the terminal tab/window title.

Works in xterm-compatible terminals: Ghostty, iTerm2, Terminal.app, Alacritty,
Kitty, WezTerm, tmux, etc.  No-op in non-TTY contexts (gateway, tests, pipes).

The agent hooks this at five points:

  1. Session init — shows existing session title or "Hermes" for fresh sessions.
  2. First user message — shows a snippet of the message until auto-title lands.
  3. Auto-title completion — swaps in the LLM-generated title.
  4. ``/title`` slash command — user-set title overrides everything.
  5. ``/new`` / ``/resume`` / ``/branch`` — resets to the new session's state.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

# Maximum length for the tab title. Long titles get truncated mid-sentence
# rather than at word boundaries to keep the heuristic dumb and fast.
MAX_TITLE_LEN = 60

# Strip ESC / CSI / OSC and other C0/C1 control characters so a nested OSC
# can't escape the title buffer (would otherwise be a terminal injection).
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_DEFAULT_TITLE = "Hermes"


def _is_capable_tty() -> bool:
    """True when the controlling terminal likely handles OSC 0/2.

    Checks ``sys.__stdout__`` (not ``sys.stdout``) because prompt_toolkit's
    ``patch_stdout`` replaces ``sys.stdout`` with a proxy whose ``isatty``
    returns False even when the real terminal is interactive.
    """
    try:
        real = sys.__stdout__
        if real is None or not real.isatty():
            return False
    except Exception:
        return False
    term = (os.environ.get("TERM") or "").lower()
    # Pipe-through-tmux/screen needs a DCS passthrough wrapper; skipping for
    # now — the title still lands on the outer terminal via OSC if supported.
    if term in ("dumb", ""):
        return False
    return True


def _sanitize(text: str) -> str:
    """Strip control chars, collapse whitespace, truncate."""
    if not text:
        return ""
    cleaned = _CTRL_RE.sub("", text)
    cleaned = " ".join(cleaned.split())  # collapse runs of whitespace
    if len(cleaned) > MAX_TITLE_LEN:
        cleaned = cleaned[: MAX_TITLE_LEN - 1].rstrip() + "…"
    return cleaned


def set_tab_title(text: Optional[str]) -> None:
    """Set the terminal tab/window title.

    Uses OSC 0 so both the icon name and the window title get updated — most
    terminals display OSC 0 as the tab title too.  BEL (``\\x07``) terminator
    is used instead of ST because a stray ``\\x1b\\`` can be rendered as
    literal text on some terminals when pasted by an outer application.

    Writes directly to ``/dev/tty`` so the escape sequence bypasses
    prompt_toolkit's ``patch_stdout`` StdoutProxy (which swallows raw OSC
    codes when the CLI is running).  Falls back to ``sys.__stdout__`` if
    ``/dev/tty`` is unavailable.

    No-op in non-TTY contexts or if ``text`` is empty after sanitization.
    """
    if not _is_capable_tty():
        return
    cleaned = _sanitize(text or "")
    if not cleaned:
        cleaned = _DEFAULT_TITLE
    payload = f"\x1b]0;{cleaned}\x07"

    # /dev/tty is the controlling terminal of the process, not affected by
    # any stdout redirection or prompt_toolkit's patch_stdout proxy.
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(payload)
            tty.flush()
        return
    except (OSError, IOError):
        pass

    # Fallback: write to the real underlying stdout (bypasses patch_stdout).
    try:
        real_stdout = sys.__stdout__
        if real_stdout is not None:
            real_stdout.write(payload)
            real_stdout.flush()
    except Exception:
        # Never let a terminal write break the CLI.
        pass


def reset_tab_title() -> None:
    """Reset the tab title back to the default."""
    set_tab_title(_DEFAULT_TITLE)
