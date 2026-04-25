"""Regression tests for SessionDB() honoring HERMES_HOME at call time.

History: ``DEFAULT_DB_PATH = get_hermes_home() / 'state.db'`` was evaluated at
module-import time, which baked in the real ``~/.hermes/state.db`` path
before tests/conftest.py could monkeypatch HERMES_HOME. As a result,
``SessionDB()`` (no path argument) wrote to the developer's real state.db
during the test suite — observed in practice as 107 stub ``source='telegram'``
sessions with ``user_id='u1'`` polluting a live database.

These tests pin the bug shut: the default path must re-resolve from the
current HERMES_HOME on every ``SessionDB()`` construction.
"""

import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest


def test_default_db_path_is_lazy(monkeypatch, tmp_path):
    """``DEFAULT_DB_PATH`` re-resolves from HERMES_HOME on each access."""
    import hermes_state

    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert hermes_state.DEFAULT_DB_PATH == home_a / "state.db"

    monkeypatch.setenv("HERMES_HOME", str(home_b))
    assert hermes_state.DEFAULT_DB_PATH == home_b / "state.db"


def test_session_db_no_args_writes_to_hermes_home(monkeypatch, tmp_path):
    """``SessionDB()`` with no args writes to ``$HERMES_HOME/state.db``."""
    fake_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(fake_home))

    from hermes_state import SessionDB

    db = SessionDB()
    try:
        assert db.db_path == fake_home / "state.db"
        assert db.db_path.exists()
        # Real ~/.hermes/state.db must NOT have been touched as a side effect.
        real = Path(os.path.expanduser("~/.hermes/state.db"))
        # We can't assert "real didn't change" portably; the tightest check
        # we can do is verify our DB is a different file on disk.
        if real.exists():
            assert db.db_path.resolve() != real.resolve()
    finally:
        db.close()


def test_session_db_no_args_respects_post_import_monkeypatch(
    monkeypatch, tmp_path
):
    """Reproduces the original pollution bug: a test that imports
    ``hermes_state`` BEFORE setting HERMES_HOME, then constructs
    ``SessionDB()``, must still get the patched path — not the path
    that was current at module-import time.
    """
    # Force a re-import to simulate "module imported with whatever
    # HERMES_HOME was at startup, then conftest sets a tmpdir".
    sys.modules.pop("hermes_state", None)
    import hermes_state  # noqa: F401  (re-imported above)

    fake_home = tmp_path / "later"
    monkeypatch.setenv("HERMES_HOME", str(fake_home))

    db = hermes_state.SessionDB()
    try:
        assert db.db_path == fake_home / "state.db"
    finally:
        db.close()


def test_session_db_isolates_telegram_writes(monkeypatch, tmp_path):
    """Concrete protection: writing a telegram session doesn't reach
    the real state.db. This is the exact pollution shape we observed."""
    fake_home = tmp_path / "isolated"
    monkeypatch.setenv("HERMES_HOME", str(fake_home))

    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(
            session_id="20990101_000000_test",
            source="telegram",
            user_id="u1",
        )
    finally:
        db.close()

    # Verify the fixture wrote to the tmpdir, not to ~/.hermes/state.db.
    fake_db = fake_home / "state.db"
    assert fake_db.exists()
    conn = sqlite3.connect(str(fake_db))
    try:
        rows = conn.execute(
            "SELECT id, source, user_id FROM sessions WHERE id = ?",
            ("20990101_000000_test",),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("20990101_000000_test", "telegram", "u1")]
