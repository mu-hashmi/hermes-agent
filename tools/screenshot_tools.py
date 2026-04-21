"""macOS screenshot tools — capture screen, specific displays, or specific windows.

Mirrors the workspace-awareness idea behind Codex's Chronicle, minus the
continuous-capture daemon: the agent takes on-demand screenshots when the user
says "look at what I'm working on", then pipes the resulting PNG path into
``vision_analyze`` to reason about the content.

macOS-only. Requires the host terminal application (Ghostty, Terminal.app,
iTerm2, etc.) to have been granted Screen Recording permission in System
Settings > Privacy & Security. The child ``screencapture`` process inherits
that permission from its parent.

Three tools, all under the ``screenshot`` toolset:

  - screen_list          — enumerate displays and visible windows
  - screenshot_display   — capture a full monitor (N = 1, 2, 3, ...)
  - screenshot_window    — capture one specific window, identified by app
                            name, title substring, or window id

Screenshots are written to ``/tmp/hermes-screens/`` as ``YYYYMMDD_HHMMSS_<kind>.png``.
Files older than 6 hours are pruned on every call (matching the Chronicle
ephemerality pattern). Tool results include the absolute path so ``vision_analyze``
can open them directly.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCREENSHOT_DIR = Path("/tmp/hermes-screens")
MAX_AGE = timedelta(hours=6)
SCREENCAPTURE = "/usr/sbin/screencapture"


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def check_screenshot_requirements() -> bool:
    """Only register on macOS with the screencapture binary present."""
    if platform.system() != "Darwin":
        return False
    return Path(SCREENCAPTURE).exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _prune_old() -> int:
    """Delete screenshots older than MAX_AGE. Returns count removed."""
    if not SCREENSHOT_DIR.exists():
        return 0
    cutoff = time.time() - MAX_AGE.total_seconds()
    removed = 0
    for p in SCREENSHOT_DIR.glob("*.png"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _output_path(kind: str) -> Path:
    _ensure_dir()
    _prune_old()
    return SCREENSHOT_DIR / f"{_timestamp()}_{kind}.png"


def _run_screencapture(args: List[str], out_path: Path) -> Optional[str]:
    """Run ``screencapture`` with the given args, writing to ``out_path``.

    Returns None on success, or a human-readable error string on failure.
    ``screencapture`` exits 0 even when it prints errors to stderr, so we
    check both the return code and stderr output plus whether the file was
    actually written.
    """
    cmd = [SCREENCAPTURE, "-x"] + args + [str(out_path)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "screencapture timed out after 15s"
    except FileNotFoundError:
        return f"screencapture binary not found at {SCREENCAPTURE}"

    stderr = (result.stderr or "").strip()

    # Screen Recording permission missing: screencapture prints exactly
    # "could not create image from display" and writes no file.
    if stderr and "could not create image" in stderr.lower():
        return (
            "Screen Recording permission missing. Open "
            "System Settings > Privacy & Security > Screen Recording, "
            "enable the terminal application (Ghostty / Terminal.app / iTerm2 / "
            "whichever is hosting this Hermes session), then fully quit and "
            "reopen that terminal. The permission does not take effect until "
            "the terminal is restarted."
        )

    if not out_path.exists() or out_path.stat().st_size == 0:
        msg = stderr or f"screencapture exited {result.returncode} without writing output"
        return msg

    return None


# ---------------------------------------------------------------------------
# Display & window enumeration (pyobjc / Quartz)
# ---------------------------------------------------------------------------

def _list_displays_raw() -> List[Dict[str, Any]]:
    """Return all active displays with resolution and position."""
    try:
        from Quartz import (
            CGGetActiveDisplayList,
            CGDisplayBounds,
            CGMainDisplayID,
        )
    except ImportError:
        return []

    err, active, count = CGGetActiveDisplayList(16, None, None)
    if err != 0:
        return []

    main_id = CGMainDisplayID()
    displays: List[Dict[str, Any]] = []
    for i, did in enumerate(active[:count]):
        b = CGDisplayBounds(did)
        displays.append({
            # screencapture -D is 1-indexed with the main display as 1
            "display_number": i + 1,
            "display_id": int(did),
            "is_main": int(did) == int(main_id),
            "width": int(b.size.width),
            "height": int(b.size.height),
            "x": int(b.origin.x),
            "y": int(b.origin.y),
        })
    return displays


def _list_windows_raw(
    *,
    include_minimized: bool = False,
    include_offscreen: bool = False,
) -> List[Dict[str, Any]]:
    """Return visible windows belonging to user-space apps.

    Filters out menubar/dock/wallpaper layers, windows owned by the WindowServer,
    and (by default) windows that are minimized or off-screen.
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListOptionAll,
            kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
    except ImportError:
        return []

    option = kCGWindowListExcludeDesktopElements
    if not include_minimized and not include_offscreen:
        option |= kCGWindowListOptionOnScreenOnly
    else:
        option |= kCGWindowListOptionAll

    raw = CGWindowListCopyWindowInfo(option, kCGNullWindowID) or []
    windows: List[Dict[str, Any]] = []
    for w in raw:
        layer = int(w.get("kCGWindowLayer", 0))
        if layer != 0:
            # layer 0 is the normal app window layer; everything else is
            # menubar / dock / system UI / tooltips and is not useful context.
            continue

        owner = w.get("kCGWindowOwnerName", "") or ""
        title = w.get("kCGWindowName", "") or ""
        wid = w.get("kCGWindowNumber")
        bounds = w.get("kCGWindowBounds") or {}

        # Skip Window Server and obvious system overlays.
        if owner in ("Window Server", "Dock", "WindowManager", "SystemUIServer"):
            continue

        windows.append({
            "window_id": int(wid) if wid is not None else None,
            "app": str(owner),
            "title": str(title),
            "width": int(bounds.get("Width", 0)),
            "height": int(bounds.get("Height", 0)),
            "x": int(bounds.get("X", 0)),
            "y": int(bounds.get("Y", 0)),
            "pid": int(w.get("kCGWindowOwnerPID", 0)),
        })

    # Sort: titled windows first, then by app name, then by size (largest first).
    windows.sort(
        key=lambda w: (
            0 if w["title"] else 1,
            w["app"].lower(),
            -(w["width"] * w["height"]),
        )
    )
    return windows


# ---------------------------------------------------------------------------
# screen_list
# ---------------------------------------------------------------------------

def screen_list_tool(*, include_minimized: bool = False) -> str:
    try:
        displays = _list_displays_raw()
        windows = _list_windows_raw(include_minimized=include_minimized)
    except Exception as e:
        logger.exception("screen_list failed")
        return tool_error(f"Failed to enumerate screen sources: {e}")

    if not displays and not windows:
        return tool_error(
            "Quartz framework unavailable. Install it in the Hermes venv: "
            "`~/.hermes/hermes-agent/venv/bin/pip install pyobjc-framework-Quartz`"
        )

    return json.dumps({
        "displays": displays,
        "windows": windows,
        "total_displays": len(displays),
        "total_windows": len(windows),
        "screenshot_dir": str(SCREENSHOT_DIR),
        "hint": (
            "To capture: screenshot_window(app=..., title_contains=...) or "
            "screenshot_display(display_number=N). "
            "Then pass the returned path to vision_analyze to actually see it."
        ),
    }, indent=2)


# ---------------------------------------------------------------------------
# screenshot_display
# ---------------------------------------------------------------------------

def screenshot_display_tool(*, display_number: int = 1) -> str:
    if not isinstance(display_number, int) or display_number < 1:
        return tool_error("display_number must be a positive integer (1 = main display)")

    displays = _list_displays_raw()
    if displays and display_number > len(displays):
        return tool_error(
            f"display_number {display_number} out of range — only "
            f"{len(displays)} display(s) connected. Call screen_list() to see them."
        )

    out = _output_path(f"display{display_number}")
    err = _run_screencapture(["-D", str(display_number)], out)
    if err:
        return tool_error(err)

    return json.dumps({
        "path": str(out),
        "display_number": display_number,
        "size_bytes": out.stat().st_size,
        "hint": f"Pass '{out}' to vision_analyze to see the screenshot.",
    })


# ---------------------------------------------------------------------------
# screenshot_window
# ---------------------------------------------------------------------------

def _match_window(
    windows: List[Dict[str, Any]],
    *,
    app: Optional[str],
    title_contains: Optional[str],
    window_id: Optional[int],
) -> List[Dict[str, Any]]:
    if window_id is not None:
        return [w for w in windows if w["window_id"] == int(window_id)]

    matches = list(windows)
    if app:
        pat = re.compile(re.escape(app), re.IGNORECASE)
        matches = [w for w in matches if pat.search(w["app"])]
    if title_contains:
        pat = re.compile(re.escape(title_contains), re.IGNORECASE)
        matches = [w for w in matches if pat.search(w["title"])]
    return matches


def screenshot_window_tool(
    *,
    app: Optional[str] = None,
    title_contains: Optional[str] = None,
    window_id: Optional[int] = None,
    include_shadow: bool = False,
) -> str:
    if not any([app, title_contains, window_id]):
        return tool_error(
            "Specify at least one of: app, title_contains, window_id. "
            "Call screen_list() first if you don't know what's open."
        )

    try:
        windows = _list_windows_raw()
    except Exception as e:
        return tool_error(f"Failed to enumerate windows: {e}")

    matches = _match_window(
        windows,
        app=app,
        title_contains=title_contains,
        window_id=window_id,
    )

    if not matches:
        available = [
            f"{w['app']}" + (f" — {w['title']}" if w['title'] else "")
            for w in windows[:15]
        ]
        return tool_error(
            "No window matched. "
            f"Open candidates (first 15): {available}. "
            "Call screen_list() for the full list."
        )

    if len(matches) > 1 and window_id is None:
        summaries = [
            {"window_id": w["window_id"], "app": w["app"], "title": w["title"]}
            for w in matches
        ]
        return tool_error(
            f"Ambiguous: {len(matches)} windows matched. "
            f"Pass window_id= to pick one. Matches: {json.dumps(summaries)}"
        )

    target = matches[0]
    wid = target["window_id"]
    if wid is None:
        return tool_error("Matched window has no window id (unexpected)")

    # Build a safe filename slug from app name.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", target["app"]).strip("-").lower() or "window"
    out = _output_path(f"window-{slug}")

    args = ["-l", str(wid)]
    if not include_shadow:
        args.insert(0, "-o")  # -o = omit window shadow

    err = _run_screencapture(args, out)
    if err:
        return tool_error(err)

    return json.dumps({
        "path": str(out),
        "app": target["app"],
        "title": target["title"],
        "window_id": wid,
        "size_bytes": out.stat().st_size,
        "hint": f"Pass '{out}' to vision_analyze to see the screenshot.",
    })


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SCREEN_LIST_SCHEMA = {
    "name": "screen_list",
    "description": (
        "Enumerate connected displays and currently-visible windows on macOS. "
        "Use this to pick a target before calling screenshot_window or "
        "screenshot_display — especially when the user says 'look at my browser' "
        "or 'check my dashboard' and you need to resolve which window they mean. "
        "Returns display numbers, resolutions, and a list of windows with "
        "{app, title, window_id, bounds}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_minimized": {
                "type": "boolean",
                "description": (
                    "If true, also include minimized or off-screen windows. "
                    "Default false — those can't be screenshotted anyway."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}

SCREENSHOT_DISPLAY_SCHEMA = {
    "name": "screenshot_display",
    "description": (
        "Capture a full monitor to a PNG file on macOS. Use this for "
        "'screenshot my screen' / 'look at my monitor 2'. "
        "Returns the file path — pass it to vision_analyze to actually see the image. "
        "For a specific app window only, prefer screenshot_window to avoid "
        "capturing unrelated context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "display_number": {
                "type": "integer",
                "description": (
                    "1 = main display, 2 = second monitor, etc. "
                    "Call screen_list to see what's connected. Default 1."
                ),
                "default": 1,
                "minimum": 1,
            },
        },
        "required": [],
    },
}

SCREENSHOT_WINDOW_SCHEMA = {
    "name": "screenshot_window",
    "description": (
        "Capture a single application window on macOS, in isolation — no "
        "surrounding desktop, no other apps. Critical when the terminal hosting "
        "this Hermes session is fullscreen: screenshot_display would just show "
        "the terminal itself, but screenshot_window(app='Arc') captures the "
        "browser on another monitor.\n\n"
        "**For websites, prefer browser_harness first.** It reads the DOM "
        "directly and is 10-50x faster than screenshot → vision. Use "
        "screenshot_window for websites only when the answer genuinely needs "
        "VISUAL reasoning: charts/graphs, image content, rendered layout or "
        "styling, visual state that contradicts the DOM, or when the user "
        "explicitly asked for a visual record. screenshot_window IS the right "
        "first move for non-browser apps (Xcode, Slack desktop, Linear "
        "desktop, Figma, Terminal windows other than the one hosting Hermes, "
        "etc.) — browser_harness can't see those.\n\n"
        "Specify the target via app name substring, window title substring, or "
        "a specific window_id from screen_list. If multiple windows match, the "
        "tool errors with the candidate list — use window_id to disambiguate. "
        "Returns the PNG path — pass it to vision_analyze to see the content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": (
                    "Application name substring, case-insensitive (e.g. 'Arc', "
                    "'Chrome', 'Xcode', 'Slack', 'Linear'). Fuzzy substring match."
                ),
            },
            "title_contains": {
                "type": "string",
                "description": (
                    "Window title substring, case-insensitive. Combined with "
                    "'app' as AND. Useful when one app has many windows."
                ),
            },
            "window_id": {
                "type": "integer",
                "description": (
                    "Exact window id from screen_list. Use this to disambiguate "
                    "when 'app' or 'title_contains' matches multiple windows."
                ),
            },
            "include_shadow": {
                "type": "boolean",
                "description": (
                    "Include the window's drop shadow in the capture. Default "
                    "false, which produces a tighter crop."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="screen_list",
    toolset="screenshot",
    schema=SCREEN_LIST_SCHEMA,
    handler=lambda args, **kw: screen_list_tool(
        include_minimized=bool(args.get("include_minimized", False)),
    ),
    check_fn=check_screenshot_requirements,
    emoji="🗂️",
)

registry.register(
    name="screenshot_display",
    toolset="screenshot",
    schema=SCREENSHOT_DISPLAY_SCHEMA,
    handler=lambda args, **kw: screenshot_display_tool(
        display_number=int(args.get("display_number", 1)),
    ),
    check_fn=check_screenshot_requirements,
    emoji="🖥️",
)

registry.register(
    name="screenshot_window",
    toolset="screenshot",
    schema=SCREENSHOT_WINDOW_SCHEMA,
    handler=lambda args, **kw: screenshot_window_tool(
        app=args.get("app"),
        title_contains=args.get("title_contains"),
        window_id=int(args["window_id"]) if args.get("window_id") is not None else None,
        include_shadow=bool(args.get("include_shadow", False)),
    ),
    check_fn=check_screenshot_requirements,
    emoji="📸",
)
