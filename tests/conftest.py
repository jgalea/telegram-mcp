"""Shared test fixtures for telegram-mcp."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch, tmp_path):
    """Keep every test away from the real ~/.telegram-mcp.

    The daemon and proxy load their policy lazily from config.json and the
    daemon appends write-tier calls to the real audit log. Without this,
    tests that dispatch a write tool (even with a mocked client) leave lines
    in the user's audit.log, and a machine configured read-only would fail
    unrelated tests.
    """
    from telegram_mcp import daemon, server
    from telegram_mcp._registry import Policy

    monkeypatch.setattr(daemon, "_POLICY", Policy())
    monkeypatch.setattr(daemon, "_WRITE_BUDGET", None)
    monkeypatch.setattr(daemon, "_RESOLVED_ALLOWLIST", None)
    monkeypatch.setattr(daemon, "_UNRESOLVED_ALLOWLIST", ())
    monkeypatch.setattr(daemon, "AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(server, "_READ_ONLY", False)
