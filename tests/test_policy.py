"""Tests for runtime policy loading (config.json + environment)."""

import logging

import pytest

from telegram_mcp._registry import (
    DESTRUCTIVE_TOOLS,
    FAIL_CLOSED_POLICY,
    LOCAL_ONLY_TOOLS,
    PEER_SEND_TOOLS,
    WRITE_TOOLS,
    is_read_only_tool,
    load_policy,
    peer_arg,
    policy_from_config,
)


class TestPolicyFromConfig:
    def test_defaults(self):
        p = policy_from_config({}, {})
        assert p.read_only is False
        assert p.send_allowlist is None and p.allowlist_active is False
        assert p.write_per_hour == 200

    def test_allowlist_keeps_raw_entries_for_resolution(self):
        """Entries are resolved through Telethon in the daemon, so spellings are
        preserved (only trimmed); nothing is normalised away here."""
        p = policy_from_config({"send_allowlist": [42, " @Alice ", "t.me/bob", "+34600000000"]}, {})
        assert p.send_allowlist == (42, "@Alice", "t.me/bob", "+34600000000")
        assert p.allowlist_active

    def test_empty_allowlist_is_active_and_empty(self):
        p = policy_from_config({"send_allowlist": []}, {})
        assert p.send_allowlist == () and p.allowlist_active

    @pytest.mark.parametrize("bad", ["@alice", {"a": 1}, [True], [""], [None], [1.5]])
    def test_malformed_allowlist_raises(self, bad):
        with pytest.raises(ValueError, match="send_allowlist"):
            policy_from_config({"send_allowlist": bad}, {})

    def test_write_budget_from_rate_limits(self):
        assert policy_from_config({"rate_limits": {"write_per_hour": 7}}, {}).write_per_hour == 7
        for bad in (0, -1, "many", True, None):
            with pytest.raises(ValueError, match="write_per_hour"):
                policy_from_config({"rate_limits": {"write_per_hour": bad}}, {})

    def test_non_object_config_raises(self):
        with pytest.raises(ValueError):
            policy_from_config([], {})


class TestEnvAndMode:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " 1 ", "enabled", "x"])
    def test_env_any_value_except_explicit_off_enables(self, val):
        assert policy_from_config({}, {"TELEGRAM_MCP_READ_ONLY": val}).read_only

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "", "no", "off", " off "])
    def test_env_explicit_off_values(self, val):
        assert not policy_from_config({}, {"TELEGRAM_MCP_READ_ONLY": val}).read_only

    def test_env_unset(self):
        assert not policy_from_config({}, {}).read_only

    @pytest.mark.parametrize(
        "mode", ["read_only", "Read_Only", " read-only ", "readonly", "RO", True]
    )
    def test_mode_read_only_spellings(self, mode):
        assert policy_from_config({"mode": mode}, {}).read_only

    @pytest.mark.parametrize("mode", [None, "", "full", "FULL", "read_write", "rw", False])
    def test_mode_full_spellings(self, mode):
        assert not policy_from_config({"mode": mode}, {}).read_only

    def test_unknown_mode_fails_closed(self, caplog):
        with caplog.at_level(logging.ERROR):
            assert policy_from_config({"mode": "wide_open"}, {}).read_only
        assert "unknown mode" in caplog.text


class TestLoadPolicyFailsClosed:
    def test_unreadable_config_gives_fail_closed_policy(self, monkeypatch, caplog):
        def boom():
            raise OSError("permission denied")

        monkeypatch.setattr("telegram_mcp.login.load_config", boom)
        with caplog.at_level(logging.ERROR):
            p = load_policy()
        assert p == FAIL_CLOSED_POLICY
        assert p.read_only and p.send_allowlist == ()
        assert "fail-closed" in caplog.text

    def test_malformed_config_gives_fail_closed_policy(self, monkeypatch):
        monkeypatch.setattr("telegram_mcp.login.load_config", lambda: {"send_allowlist": "x"})
        assert load_policy() == FAIL_CLOSED_POLICY

    def test_good_config_loads(self, monkeypatch):
        monkeypatch.setattr("telegram_mcp.login.load_config", lambda: {"send_allowlist": [1]})
        monkeypatch.delenv("TELEGRAM_MCP_READ_ONLY", raising=False)
        p = load_policy()
        assert p.send_allowlist == (1,) and not p.read_only


class TestTierConsistency:
    def test_destructive_and_peer_send_are_write(self):
        assert DESTRUCTIVE_TOOLS <= WRITE_TOOLS
        assert set(PEER_SEND_TOOLS) <= WRITE_TOOLS

    def test_write_tools_are_not_read_only(self):
        for name in WRITE_TOOLS | {"clear_cache"}:
            assert not is_read_only_tool(name), name

    def test_local_only_tools_never_write_to_telegram(self):
        assert not (LOCAL_ONLY_TOOLS & WRITE_TOOLS)

    def test_every_write_tool_has_a_peer_arg_for_audit(self):
        for name in WRITE_TOOLS - {"create_group", "create_channel"}:
            assert peer_arg(name), name

    def test_every_content_delivering_tool_is_peer_gated(self):
        """Any tool whose client method takes a text/caption/file plus a chat
        must be in PEER_SEND_TOOLS, otherwise the allowlist has a hole."""
        import inspect

        from telegram_mcp.client import TelegramMCPClient

        for name in WRITE_TOOLS:
            params = set(inspect.signature(getattr(TelegramMCPClient, name)).parameters)
            delivers = params & {"text", "caption", "file_path", "emoji", "lat", "message_ids"}
            if delivers and name not in {"delete_message", "set_chat_photo"}:
                assert name in PEER_SEND_TOOLS, name
