# Security

## Threat model

**A Telegram session string is equivalent to full access to that Telegram
account** — reading every chat, and (were the tools not restricted) sending
messages, joining/leaving chats, changing profile data, and more. Everything
below follows from that one fact.

### What this project does about it

- **Read-only by default.** The server runs with `TELEGRAM_EXPOSED_TOOLS=read-only`,
  which keeps only tools annotated `readOnlyHint=True` registered — sending
  messages and any state-changing action are not reachable through this
  project's default configuration. Don't change this setting without
  understanding that it removes that boundary.
- **Secrets live in exactly one place:** `~/telegram-mcp-ag/config.env`,
  created with mode `600` (owner read/write only). Client configuration files
  (`.mcp.json`, `~/.codex/config.toml`, `claude_desktop_config.json`) only ever
  get a path to the installed binary, never `TELEGRAM_SESSION_STRING`,
  `api_id`, or `api_hash` — those files are far more likely to be committed to
  a repository or shared by accident.
- **One session, one machine.** Telegram detects a session string reused from
  a second IP and kills it with `AUTH_KEY_DUPLICATED`. This project treats
  that as a feature, not a bug to work around: each machine gets its own
  login (`install.sh --relogin` / `install.ps1 -Relogin`) rather than a
  copied string.
- **Login secrets never travel through the chat.** The QR login flow
  (`telegram_login_start`/`telegram_login_check`, used by the Claude Desktop
  bundle and the `setup-telegram-mcp` skill) puts only a one-shot login token
  in the conversation — no phone number, no login code, no session string.
  If the account has a cloud password (2FA), it's requested through a native
  OS dialog (`zenity`/`kdialog`, `osascript`, or a PowerShell secure prompt),
  used once, and never written to disk, chat, or a keychain. `install.sh` /
  `install.ps1` ask for the same password directly on their own terminal for
  the same reason.
- **The upstream pin is a deliberate boundary.** `chigwell/telegram-mcp` is
  pinned to a specific commit and imported with explicit names, not
  `import *` — an upstream update is a manual, reviewed step (see
  CONTRIBUTING.md), not something that changes this project's read-only
  guarantee silently.

### What you should do

- Treat `~/telegram-mcp-ag/config.env` like a password: never paste its
  contents into a chat, issue, or screen share.
- If you ever suspect the session string leaked (accidental commit, shared
  screen, compromised machine): open Telegram → **Settings → Devices**, find
  the matching device (named `telegram-mcp-ag (<hostname>)`), and end that
  session immediately. This takes effect instantly, before you even need to
  touch the machine running the server.
- Prefer a dedicated login over reusing one across machines you don't fully
  trust equally — a VPS and a laptop should each have their own session.
- Keep `TELEGRAM_EXPOSED_TOOLS=read-only` unless you have a specific,
  understood reason to change it.

## Reporting a vulnerability

Open a GitHub issue at
[agroznykh/telegram-mcp-ag/issues](https://github.com/agroznykh/telegram-mcp-ag/issues).
If the report involves a credential that may already be exposed, revoke it in
Telegram first (see above) and mention that you've done so in the report.
