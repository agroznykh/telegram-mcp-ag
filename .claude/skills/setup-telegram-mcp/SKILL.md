---
name: setup-telegram-mcp
description: Install and register the telegram-mcp-ag MCP server for Claude Code entirely from this chat, without the user opening a terminal. Use when the user asks to connect/set up/install Telegram in Claude Code, or asks to read Telegram messages and no telegram-mcp-ag tools are registered yet.
---

Second, lighter-weight installation path for people who are already talking to
Claude Code: it does everything `install.sh` does for the Claude Code client,
but by driving the same steps from inside this chat instead of asking the user
to paste a `curl | bash` line into a terminal. It is a layer on top of
`install.sh`, not a replacement — `install.sh` is still the way to register
Codex/ChatGPT Desktop/Claude Desktop, and it has `--relogin`/`--uninstall`
maintenance flows this skill does not attempt to reproduce. If the user asks
for those, point them at `install.sh` (see the project README).

## Why this can't just shell out to `install.sh`

`install.sh` reads `api_id`/`api_hash` and drives the Telegram QR login through
`/dev/tty`, which a `Bash` tool call from Claude Code does not have. This skill
sidesteps the login step entirely instead of trying to fake a TTY: it writes
`config.env` with credentials but **no session line**, which makes the server
boot in its built-in login-only mode. In that mode it exposes exactly two
tools, `telegram_login_start` and `telegram_login_check`, which run the QR
login through the chat itself (this is the same mechanism the `.mcpb` bundle
for Claude Desktop uses — see `login.py` and `PLAN.md` step 6б). So the split
is: this skill does everything up to registering the server, and the *next*
chat session finishes the login by literally calling those two tools.

## Steps

1. **Check whether it's already set up.** If Telegram tools (e.g. `list_chats`,
   `get_history`, or `telegram_login_start`) are already visible in this
   session, nothing to install — tell the user and, if only login tools are
   visible, jump straight to step 7.

2. **Check prerequisites** with `Bash` (no TTY needed for any of this):
   - `command -v python3.13 python3.12 python3.11 python3.10 python3` — first
     one found that reports version ≥ 3.10 is usable.
   - If none qualify, check `command -v uv`. If neither exists, ask the user
     (in chat, not a terminal prompt) whether to install `uv` with
     `curl -LsSf https://astral.sh/uv/install.sh | sh` (no sudo, installs into
     `~/.local/bin`). If they decline, point them at
     https://www.python.org/downloads/ and stop here.

3. **Create the venv and install the package**, mirroring `install.sh`'s
   `setup_python_and_venv`/`install_package`:
   ```bash
   mkdir -p ~/telegram-mcp-ag
   <python> -m venv ~/telegram-mcp-ag/.venv   # or: uv venv --seed --python 3.12 ~/telegram-mcp-ag/.venv
   ~/telegram-mcp-ag/.venv/bin/python -m pip install --upgrade pip -q
   ~/telegram-mcp-ag/.venv/bin/python -m pip install -q "git+https://github.com/agroznykh/telegram-mcp-ag.git@main"
   ```
   No tagged release exists yet, so `@main` is correct — check the README for
   whether a version tag should be used instead.

4. **Ask for Telegram API credentials in the chat.** Tell the user to open
   https://my.telegram.org/apps (log in with their phone number, open "API
   development tools") and give you `api_id` and `api_hash`. Validate before
   writing anything: `api_id` must be all digits; `api_hash` is normally 32
   hex characters — if it isn't, confirm with the user before using it as-is
   rather than rejecting it outright (some accounts show it differently).

5. **Write `config.env` without a session line**, reusing the project's own
   writer so permissions and quoting match what the server expects:
   ```bash
   ~/telegram-mcp-ag/.venv/bin/python -c '
   from telegram_mcp_ag import config
   config.write_values({
       "TELEGRAM_API_ID": "<api_id>",
       "TELEGRAM_API_HASH": "<api_hash>",
       "TELEGRAM_EXPOSED_TOOLS": "read-only",
       "TELEGRAM_DEVICE_MODEL": "telegram-mcp-ag (claude-code-setup)",
   })'
   ```
   This creates `~/telegram-mcp-ag/config.env` at mode `600`. Deliberately omit
   `TELEGRAM_SESSION_STRING` — its absence is what puts the server into
   login-only mode on next launch.

6. **Register the server with Claude Code:**
   ```bash
   claude mcp remove -s user telegram-mcp-ag 2>/dev/null || true
   claude mcp add -s user telegram-mcp-ag -- ~/telegram-mcp-ag/.venv/bin/telegram-mcp-ag
   ```

7. **Hand off to a new session.** Claude Code loads MCP servers at startup, so
   this chat cannot pick up the newly-registered server itself. Tell the user
   plainly: start a new Claude Code session (new window/tab, or exit and
   relaunch `claude`), then in that session say something like "connect my
   Telegram account". The server will come up in login-only mode and expose
   `telegram_login_start`/`telegram_login_check`; calling
   `telegram_login_start` shows a QR code right in the chat (also opened as a
   local image file, plus a `tg://login` link as a fallback) to scan from
   Telegram → Settings → Devices → Link Desktop Device, and
   `telegram_login_check` finishes the login once scanned. Once that
   completes, reading and transcription tools become available in that same
   session — no further restart needed.

8. **If the account has a 2FA cloud password**, `telegram_login_check` will
   try a native OS password dialog automatically. On a headless machine where
   no dialog can appear, it will say so and point back at `install.sh`, which
   asks for the password over its own terminal `/dev/tty` — that's the correct
   fallback, not something to work around here.
