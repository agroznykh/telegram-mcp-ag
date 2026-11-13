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
