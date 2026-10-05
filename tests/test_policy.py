"""Tests for runtime policy loading (config.json + environment)."""

import pytest

from telegram_mcp._registry import (
    DESTRUCTIVE_TOOLS,
    LOCAL_ONLY_TOOLS,
    PEER_SEND_TOOLS,
    WRITE_TOOLS,
    is_read_only_tool,
    peer_arg,
    policy_from_config,
)


class TestPolicyFromConfig:
    def test_defaults(self):
        p = policy_from_config({}, {})
        assert p.read_only is False
        assert p.send_allowlist is None
        assert p.write_per_hour == 200
        assert p.peer_allowed(123) and p.peer_allowed("@anyone")

    def test_allowlist_normalises_ids_and_usernames(self):
        p = policy_from_config({"send_allowlist": [42, "43", "@Alice", "bob"]}, {})
        assert p.send_allowlist == frozenset({42, 43, "@alice", "@bob"})
        assert p.peer_allowed("42") and p.peer_allowed("ALICE") and p.peer_allowed("@Bob")
        assert not p.peer_allowed(44) and not p.peer_allowed("@carol")
        assert not p.peer_allowed(None) and not p.peer_allowed(True)

    def test_empty_allowlist_refuses_everyone(self):
        p = policy_from_config({"send_allowlist": []}, {})
        assert p.send_allowlist == frozenset()
        assert not p.peer_allowed(42)

    def test_allowlist_must_be_list(self):
        with pytest.raises(ValueError, match="send_allowlist"):
            policy_from_config({"send_allowlist": "@alice"}, {})

    def test_write_budget_from_rate_limits(self):
        assert policy_from_config({"rate_limits": {"write_per_hour": 7}}, {}).write_per_hour == 7
        with pytest.raises(ValueError, match="write_per_hour"):
            policy_from_config({"rate_limits": {"write_per_hour": 0}}, {})

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_env_truthy_values(self, val):
        assert policy_from_config({}, {"TELEGRAM_MCP_READ_ONLY": val}).read_only

    @pytest.mark.parametrize("val", ["0", "false", "", "no"])
    def test_env_falsy_values(self, val):
        assert not policy_from_config({}, {"TELEGRAM_MCP_READ_ONLY": val}).read_only


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
