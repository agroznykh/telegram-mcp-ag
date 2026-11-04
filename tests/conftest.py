import os
import tempfile

# Importing telegram_mcp_ag.server runs config.load() against ~/telegram-mcp-ag/
# config.env, and load() writes default keys into it. Pointing the home folder at
# a throwaway one keeps the suite away from the real installation of whoever runs
# it. Done before any test module imports the package: CONFIG_PATH is fixed at
# import. Subprocess tests inherit it.
_TEST_HOME = tempfile.mkdtemp(prefix="telegram-mcp-ag-test-home-")
os.environ["HOME"] = _TEST_HOME
os.environ["USERPROFILE"] = _TEST_HOME

# Importing telegram_mcp_ag.server asks Telegram whether the configured session
# is still alive, and clears it from config.env if not. The tests import it with
# made-up sessions: that check would go to the network and could wipe the
# session of whoever runs the suite. Not TELEGRAM_-prefixed, so the fixtures
# that strip those variables keep it, and so do the subprocess tests.
os.environ["MCP_AG_SKIP_SESSION_CHECK"] = "1"
