# Changelog

## 0.1.2 — 2026-06-17

### Security
- **Fence now escapes opening markers, not just closing ones.** A Telegram message could previously embed a second `[TELEGRAM MESSAGE - ...]` opening tag to forge a nested trusted block and slip injected instructions past the fence. Both opening and closing markers are now escaped before wrapping.
- **Config dir and Unix socket are no longer briefly world-accessible.** The daemon created `~/.telegram-mcp/` with default (`0o755`) permissions when it won the creation race, and the Unix socket existed at default perms between creation and `chmod`. On a multi-user machine another local user could read the session file or connect to the unauthenticated socket. The dir is now created `0o700` and the socket is created under a restrictive umask.
- **Destructive-action confirmation no longer echoes raw tool args** back to the model, which could reinforce injected instructions (e.g. an attacker-supplied `to_chat`/`message_ids`).

### Changed
- The bare `telegram-mcp` command now starts the MCP server (same as `telegram-mcp serve`), so MCP clients can launch it without a subcommand.
- Published to PyPI as `telegram-mcp-jgalea` (the `telegram-mcp` name was taken); the installed command is still `telegram-mcp`.
