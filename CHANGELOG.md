# Changelog

## 0.2.1 (2026-10-05)

### Security
- **`send_allowlist` compares resolved peers, not spellings.** The 0.2.0 gate normalised the caller's string against the config entries, which neither equated the many spellings Telethon accepts for one peer (`@Name`, `name`, `t.me/name`, phone, bare id, `-100` id) nor told a user id from a channel with the same number. Recipient and entries now go through Telethon to marked peer ids; an unresolvable recipient is refused, an unresolvable entry matches nothing.
- **Policy gates fail closed.** Any exception while evaluating the allowlist or budget refuses the call. A config.json that cannot be read or parsed yields read-only with an empty allowlist. An unknown `mode` is read-only. `TELEGRAM_MCP_READ_ONLY` is on for any value other than `0`/`false`/`no`/`off`/empty.
- **The daemon inherits `TELEGRAM_MCP_READ_ONLY` from the session that starts it.** 0.2.0 stripped it, which left the env var enforced only by the proxy, and the proxy can be bypassed by anything that can open the Unix socket.

### Added
- `get_status` reports `read_only`, `send_allowlist` (entry and resolved counts, never the entries), `write_per_hour` and `write_calls_last_hour`.

## 0.2.0 (2026-10-05)

### Security
- **Cache reads no longer return `raw_json`.** `search_messages`, `search_regex`, `today_messages` and `export_cached_messages` selected every column, so the unfenced original text and sender rode along next to the fenced copy. Reads now select explicit columns.
- **Everything that reaches the model is fenced.** `export_chat`, `export_cached_messages`, `get_scheduled_messages`, the chat names in `sync_messages`, download filenames, media captions and forward origins were returned raw. Exports now write the full file to `~/.telegram-mcp/downloads/` and return the path plus a fenced summary.
- **Fence escaping is case-insensitive, whitespace-tolerant and catches fullwidth/lookalike brackets.** It also now catches the exact opening line the fence itself emits, which the old `[A-Z ]+` pattern missed because of the hyphen.
- **`confirm` must be the JSON boolean `true`.** The gate was truthy, so `"yes"`, `1` and even `"false"` passed it. `get_invite_link`, `add_participant`, `create_group`, `create_channel`, `set_chat_title`, `set_chat_description` and `set_chat_photo` are now gated too.
- **Read-only mode**, via `"mode": "read_only"` in config.json (daemon-wide) or `TELEGRAM_MCP_READ_ONLY=1` (one session), enforced in the daemon.
- **Optional `send_allowlist`** in config.json: sends, forwards, files, voice notes and locations to peers outside it are refused in the daemon. Not settable by any tool.
- **Downloads never overwrite or follow symlinks.** The server now creates the destination itself with `O_EXCL|O_NOFOLLOW` and a ` (n)` suffix on collision, and hands Telethon an open file instead of a directory.
- **`search_regex` is bounded**: 256-char patterns, 20,000-row scans, a per-match and per-search timeout (via the `regex` module), run in a worker thread.
- **Hourly write budget** (`rate_limits.write_per_hour`, default 200) and an append-only **audit log** at `~/.telegram-mcp/audit.log` (0600, rotates at 5 MB).
- Config dir is created `0700` on every path; Telethon logging is capped at WARNING and `daemon.log` rotates at 5 MB.

### Added
- MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on every tool.
- `get_status` reports package version, git sha (when run from a checkout), source mtime and daemon pid.
- `forward_from` / `forward_from_id` on forwarded messages.

### Changed
- `export_chat` and `export_cached_messages` return `{path, format, count, date_from, date_to, senders}` instead of the messages.
- Download results return `dir` plus fenced `path`, `filename` and `original_filename`.

## 0.1.2 — 2026-06-17

### Security
- **Fence now escapes opening markers, not just closing ones.** A Telegram message could previously embed a second `[TELEGRAM MESSAGE - ...]` opening tag to forge a nested trusted block and slip injected instructions past the fence. Both opening and closing markers are now escaped before wrapping.
- **Config dir and Unix socket are no longer briefly world-accessible.** The daemon created `~/.telegram-mcp/` with default (`0o755`) permissions when it won the creation race, and the Unix socket existed at default perms between creation and `chmod`. On a multi-user machine another local user could read the session file or connect to the unauthenticated socket. The dir is now created `0o700` and the socket is created under a restrictive umask.
- **Destructive-action confirmation no longer echoes raw tool args** back to the model, which could reinforce injected instructions (e.g. an attacker-supplied `to_chat`/`message_ids`).

### Changed
- The bare `telegram-mcp` command now starts the MCP server (same as `telegram-mcp serve`), so MCP clients can launch it without a subcommand.
- Published to PyPI as `telegram-mcp-jgalea` (the `telegram-mcp` name was taken); the installed command is still `telegram-mcp`.
