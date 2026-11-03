import os

# Importing telegram_mcp_ag.server asks Telegram whether the configured session
# is still alive, and clears it from config.env if not. The tests import it with
# made-up sessions: that check would go to the network and could wipe the
# session of whoever runs the suite. Not TELEGRAM_-prefixed, so the fixtures
# that strip those variables keep it, and so do the subprocess tests.
os.environ["MCP_AG_SKIP_SESSION_CHECK"] = "1"
