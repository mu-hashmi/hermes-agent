"""Regression test: post-compression message flush is durable.

History: when context compression fired, run_agent created the new
continuation session row in state.db, then set ``_last_flushed_db_idx = 0``
without flushing the compressed message list. The compressed summary lived
only in ``self.conversation_history`` until the next agent-loop iteration's
flush. If the process died between those two events — Ctrl+C interrupting
an already-blocking ``shutdown_mcp_servers``, OOM, crash — the new session
row had zero messages and the entire compressed transcript was lost.

Observed in production: session ``20260425_145249_e128dc`` ended up with
0 messages while its parent had 789. Compressor produced a 66-message
summary, the row was created, then a Ctrl+C during MCP shutdown killed
the process before any message flush.

This test pins the bug shut by calling the **real** ``_compress_context``
on a minimally-stubbed AIAgent. The compressor's LLM call is stubbed
(no network), but every persistence path runs through the actual
production code in ``run_agent.py``. After ``_compress_context`` returns,
the new session row must contain messages on disk — proven by re-opening
SQLite from scratch.
"""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _build_agent(tmp_path: Path):
    """Construct an AIAgent with the minimal real state needed for
    ``_compress_context`` to run end-to-end against a real SessionDB."""
    from hermes_state import SessionDB
    from agent.context_compressor import ContextCompressor
    from run_agent import AIAgent
    from tools.todo_tool import TodoStore

    db = SessionDB(tmp_path / "state.db")

    old_id = "20990101_111111_old"
    db.create_session(session_id=old_id, source="cli", model="anthropic/claude-test")
    for i in range(10):
        db.append_message(
            old_id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"old turn content #{i} " * 50,  # make it look big
        )

    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent.session_id = old_id
    agent.model = "anthropic/claude-test"
    agent.platform = None
    agent.provider = "anthropic"
    agent._last_flushed_db_idx = 0
    agent.logs_dir = tmp_path
    agent.session_log_file = tmp_path / f"session_{old_id}.json"

    # Real compressor with a stubbed _generate_summary so no network call fires.
    compressor = ContextCompressor(
        model="anthropic/claude-test", config_context_length=200_000
    )
    compressor._generate_summary = MagicMock(
        return_value="[CONTEXT COMPACTION] Earlier turns were compacted into a structured handoff."
    )
    agent.context_compressor = compressor

    # Things _compress_context touches that need stubs (no behavior under test).
    agent._memory_manager = None
    agent._todo_store = TodoStore()
    agent._invalidate_system_prompt = MagicMock()
    agent._build_system_prompt = MagicMock(return_value="system prompt body")
    agent._cached_system_prompt = None
    agent._apply_persist_user_message_override = MagicMock()
    agent.commit_memory_session = MagicMock()
    agent._emit_warning = MagicMock()
    agent.log_prefix = ""
    agent._vprint = MagicMock()
    return agent, db, old_id


def _build_messages_above_threshold(min_count: int = 50):
    """Build a conversation big enough to actually trigger compression."""
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(min_count):
        msgs.append({"role": "user", "content": f"User turn #{i} " * 100})
        msgs.append({"role": "assistant", "content": f"Assistant turn #{i} " * 100})
    return msgs


def test_compression_messages_durable_after_split(tmp_path):
    """After ``_compress_context`` returns, the compressed messages must
    be persisted to the new session row in SQLite, not deferred. Re-opens
    the DB from disk to prove durability is on-disk and not just cache."""
    agent, db, old_id = _build_agent(tmp_path)
    messages = _build_messages_above_threshold(min_count=80)
    system_msg = "You are a helpful assistant."

    compressed, new_prompt = agent._compress_context(
        messages, system_msg, approx_tokens=150_000
    )

    new_session_id = agent.session_id
    db.close()

    # Re-open from scratch — proves persistence at the file layer.
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
        (new_session_id,),
    ).fetchall()

    parent_row = conn.execute(
        "SELECT end_reason, ended_at FROM sessions WHERE id = ?",
        (old_id,),
    ).fetchone()
    cont_row = conn.execute(
        "SELECT parent_session_id, end_reason FROM sessions WHERE id = ?",
        (new_session_id,),
    ).fetchone()
    conn.close()

    # The bug under test: must NOT be empty.
    assert len(rows) > 0, (
        f"Continuation session {new_session_id} has 0 messages on disk. "
        f"This is the original durability bug — _compress_context created "
        f"the new session row but did not flush messages before returning."
    )
    # Same number as the in-memory compressed list returned by the function.
    assert len(rows) == len(compressed), (
        f"Persisted {len(rows)} messages but compressor returned {len(compressed)}. "
        f"Off-by-one in the flush is also a durability concern."
    )

    # Sanity: parent marked compressed, continuation linked back.
    assert parent_row[0] == "compression"
    assert parent_row[1] is not None
    assert cont_row[0] == old_id
    assert cont_row[1] is None


def test_compression_continuation_resumes_with_messages(tmp_path):
    """End-to-end check: after compression, ``get_messages_as_conversation``
    on the continuation returns the compressed transcript. This is what the
    CLI's resume path actually calls."""
    agent, db, old_id = _build_agent(tmp_path)
    messages = _build_messages_above_threshold(min_count=80)

    agent._compress_context(messages, "system", approx_tokens=150_000)
    new_session_id = agent.session_id
    restored = db.get_messages_as_conversation(new_session_id)
    db.close()

    assert len(restored) > 0
    # First message should be the compaction marker (or the system prompt).
    contents = [m.get("content", "") for m in restored]
    assert any("COMPACTION" in c.upper() or "COMPACT" in c.upper() for c in contents), (
        f"Restored messages don't contain the compaction marker. "
        f"First content: {contents[0][:200] if contents else '(none)'}"
    )
