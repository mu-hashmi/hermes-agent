"""Browser Harness tool — drive a live Chrome via the DevTools Protocol.

Thin wrapper around the ``browser-harness-js`` CLI (Bun server from the
browser-use ``cdp`` skill). One persistent CDP session is held by the server
across calls, so ``session``, the active target, and any ``globalThis.*``
values survive between invocations.

The tool is a single JS-evaluation surface on purpose — browser-harness-js
exposes 652 typed CDP methods through ``session.<Domain>.<method>(params)``,
and wrapping those into discrete Hermes tools would defeat the "protocol is
the API" design it's built around. Instead: one `browser_harness` entry in
the tool list that the agent always sees, with the full CDP reference
auto-loaded into context on first call so the agent actually knows how to
use it.

Requires:
  - ``browser-harness-js`` on PATH (from ``git clone https://github.com/browser-use/browser-harness-js``)
  - Bun (auto-installed by the CLI on first run)
  - A running Chromium-based browser with remote debugging enabled (or the
    agent calls ``await session.connect()`` which auto-detects and prompts the
    user once through ``chrome://inspect/#remote-debugging``)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Set

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# Where the skill clone installed SKILL.md — auto-loaded on first call so the
# agent has the full 652-method reference in context.
SKILL_MD_PATH = Path.home() / ".hermes" / "skills" / "cdp" / "SKILL.md"

# Task IDs we've already loaded the skill for this process lifetime.
# Each Hermes conversation has a distinct task_id; loading once per task
# prevents re-dumping the 3.3k-token reference on every CDP call.
_SKILL_LOADED: Set[str] = set()

# Generous default — CDP ops like ``Page.navigate`` can legitimately take
# tens of seconds on slow pages. Control commands (--status, --stop) are
# capped lower inside the handler.
DEFAULT_EVAL_TIMEOUT = 120
CONTROL_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _cli_path() -> Optional[str]:
    return shutil.which("browser-harness-js")


def check_browser_harness_requirements() -> bool:
    """Only register if the browser-harness-js CLI is on PATH."""
    return _cli_path() is not None


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------

def _run_cli(
    args: list[str],
    stdin_data: Optional[str] = None,
    timeout: int = DEFAULT_EVAL_TIMEOUT,
) -> subprocess.CompletedProcess:
    cli = _cli_path()
    if cli is None:
        raise FileNotFoundError("browser-harness-js not on PATH")
    return subprocess.run(
        [cli, *args],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _load_skill() -> Optional[str]:
    try:
        return SKILL_MD_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("CDP skill not found at %s — first-call reference will be omitted", SKILL_MD_PATH)
        return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

VALID_ACTIONS = {"eval", "status", "start", "stop", "restart"}


def browser_harness_tool(
    js: Optional[str] = None,
    action: str = "eval",
    task_id: Optional[str] = None,
) -> str:
    action = (action or "eval").lower()
    if action not in VALID_ACTIONS:
        return tool_error(
            f"Invalid action '{action}'. Valid: {sorted(VALID_ACTIONS)}"
        )

    # --- Control commands (server lifecycle) ---
    if action != "eval":
        try:
            r = _run_cli([f"--{action}"], timeout=CONTROL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return tool_error(f"browser-harness-js --{action} timed out")
        except FileNotFoundError as e:
            return tool_error(str(e))
        return json.dumps({
            "action": action,
            "exit_code": r.returncode,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
        })

    # --- Eval mode ---
    if not js or not js.strip():
        return tool_error(
            "Provide a 'js' snippet to evaluate. "
            "Start sessions with `await session.connect()` — auto-detects your "
            "running Chrome and attaches to it. See the cdp skill reference "
            "included in the first tool result for all 652 CDP methods."
        )

    # Transport rule (matches browser-harness-js CLI behavior):
    #   - single-expression, no newlines → pass as arg1, auto-returns
    #   - multi-line → pass via stdin, agent must write `return X` explicitly
    try:
        if "\n" in js:
            r = _run_cli([], stdin_data=js, timeout=DEFAULT_EVAL_TIMEOUT)
        else:
            r = _run_cli([js], timeout=DEFAULT_EVAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return tool_error(
            f"browser-harness-js eval timed out after {DEFAULT_EVAL_TIMEOUT}s. "
            "Long-running ops (page loads, waitFor) may need the snippet to "
            "pass an explicit `timeoutMs` to CDP methods."
        )
    except FileNotFoundError as e:
        return tool_error(str(e))

    payload = {
        "exit_code": r.returncode,
        "stdout": r.stdout.rstrip("\n"),
        "stderr": r.stderr.rstrip("\n"),
    }

    # Auto-load the CDP reference on first call per task.
    # Included inline so the agent can read it in the same turn — no second
    # round-trip needed. ~3.3k tokens, loaded once per conversation.
    if task_id and task_id not in _SKILL_LOADED:
        _SKILL_LOADED.add(task_id)
        skill = _load_skill()
        if skill:
            payload["cdp_reference"] = skill
            payload["_note"] = (
                "The full CDP skill reference is included in this first call "
                "as 'cdp_reference'. Subsequent calls in this conversation "
                "will omit it — refer back to this message when you need "
                "method signatures, connection recipes, or edge-case handling."
            )

    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

BROWSER_HARNESS_SCHEMA = {
    "name": "browser_harness",
    "description": (
        "Drive the user's LIVE Chrome browser via the Chrome DevTools Protocol. "
        "Runs arbitrary JS snippets through the browser-harness-js CLI, which "
        "holds one persistent CDP session across calls. `session`, the active "
        "target, and any `globalThis.*` values survive between invocations.\n\n"
        "Use this when the user wants the agent to: read what's in their "
        "browser, navigate tabs, click/type/scroll on pages, extract DOM "
        "content, inspect network requests, capture screenshots of a tab, or "
        "automate any web flow — using their REAL browser (existing login "
        "cookies, extensions, profile) not a headless one. This is NOT the "
        "browser toolset (browserbase/chromium) — it attaches to the user's "
        "already-running Chrome/Arc/Brave/etc.\n\n"
        "First call in a conversation: start with `await session.connect()` — "
        "auto-detects running Chromium browsers via DevToolsActivePort. If no "
        "browser has remote debugging enabled, tell the user to open "
        "chrome://inspect/#remote-debugging, tick 'Discover network targets', "
        "and click Allow.\n\n"
        "The full CDP reference (652 typed methods, connection recipes, "
        "target routing, event handling, interaction recipes for dropdowns/"
        "iframes/uploads/downloads/shadow-DOM/scroll/viewport) is included in "
        "the response to the FIRST call in each conversation, under "
        "'cdp_reference'. Read that before issuing complex snippets.\n\n"
        "Single-line snippets auto-return their final expression. Multi-line "
        "snippets must use `return X` explicitly. Examples:\n"
        "  js='await session.connect()'\n"
        "  js='await session.Page.navigate({url:\\\"https://example.com\\\"})'\n"
        "  js='(await listPageTargets()).length'\n"
        "  js='const tabs = await listPageTargets();\\nglobalThis.tid = tabs[0].targetId;\\nawait session.use(globalThis.tid);\\nreturn globalThis.tid;'\n\n"
        "Output: JSON with stdout, stderr, exit_code. Non-empty stdout is the "
        "result of the JS expression.\n\n"
        "Security: this tool runs arbitrary JS in the user's browser, which "
        "can read cookies, form fields, and any authenticated page content. "
        "Increases prompt injection risk from page content. Use deliberately."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "js": {
                "type": "string",
                "description": (
                    "JS snippet to evaluate. Has access to globals: `session` "
                    "(persistent CDP Session with all 56 CDP domains mounted), "
                    "`listPageTargets()`, `detectBrowsers()`, `resolveWsUrl()`, "
                    "`CDP` namespace, and `globalThis` for cross-call state. "
                    "Required when action='eval' (the default)."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["eval", "status", "start", "stop", "restart"],
                "description": (
                    "eval (default): run the `js` snippet. "
                    "status: check if the REPL server is running. "
                    "start: explicitly start the server (usually unnecessary — "
                    "`eval` auto-starts it). "
                    "stop: shut down the server (drops session state). "
                    "restart: stop + start fresh."
                ),
                "default": "eval",
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="browser_harness",
    toolset="browser_harness",
    schema=BROWSER_HARNESS_SCHEMA,
    handler=lambda args, **kw: browser_harness_tool(
        js=args.get("js"),
        action=args.get("action", "eval"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_harness_requirements,
    emoji="🧭",
)
