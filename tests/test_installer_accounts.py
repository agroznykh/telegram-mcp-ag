"""Account handling in install.sh: flags, adding an account, signing one in again.

``install.sh`` can be sourced (it then defines everything and skips ``main``), so
each test runs the real functions in bash against a throwaway home folder. The
installed ``telegram-mcp-ag-login`` is the real one for everything that works
offline (``--list-accounts``, ``--rename-default``, ``--set-default``): only the
sign-in itself, which needs Telegram, is replaced by a stand-in that writes a
dummy session for the label it is asked for. The person's answers come from a
file. No network, no Telegram.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from telegram_mcp_ag import config, relogin

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="install.sh targets macOS/Linux",
)

# Not resolved: a venv's python is a symlink to the interpreter it was made from.
VENV_DIR = Path(sys.executable).absolute().parent.parent
LOGIN_BIN = Path(sys.executable).parent / "telegram-mcp-ag-login"

API_LINES = "TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n"

PRELUDE = r"""
set -euo pipefail
export TELEGRAM_MCP_AG_REF=test
source "$INSTALL_SH"
VENV_DIR="$REAL_VENV"
CONFIG_PATH="$HOME/telegram-mcp-ag/config.env"
TTY_DEVICE=/dev/null
exec 3<"$ANSWERS"
ask() { local r=""; IFS= read -r r <&3 || true; printf -v "$2" '%s' "$r"; }
confirm() { local r=""; IFS= read -r r <&3 || true; [[ "$r" =~ ^[YyДд]$ ]]; }
_run_login_once() {
    echo "login:${1:-}" >>"$CALLS"
    [[ "${FAKE_LOGIN_RC:-0}" -eq 0 ]] || return "$FAKE_LOGIN_RC"
    "$REAL_VENV/bin/python" -c '
import sys
from telegram_mcp_ag import config
config.write_session(sys.argv[1], "new-session-for-" + sys.argv[1], config.CONFIG_PATH)
' "${1:-default}"
}
"""


class Sandbox:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.config_path = self.home / "telegram-mcp-ag" / "config.env"
        self.calls = tmp_path / "calls.log"
        self.answers = tmp_path / "answers.txt"
        self.calls.write_text("")
        self.answers.write_text("")

    def seed(self, sessions: dict, extra: str = "") -> None:
        """A config.env with api_id/api_hash and the given {key: value} sessions."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"{key}={value}\n" for key, value in sessions.items())
        self.config_path.write_text(API_LINES + lines + extra, encoding="utf-8")
        self.config_path.chmod(0o600)

    def env(self) -> dict:
        # No TELEGRAM_* variables from the test process (other tests leave a
        # session string there): the login command merges the environment into
        # what it reads from the file.
        inherited = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
        return {
            **inherited,
            "HOME": str(self.home),
            "INSTALL_SH": str(INSTALL_SH),
            "REAL_VENV": str(VENV_DIR),
            "ANSWERS": str(self.answers),
            "CALLS": str(self.calls),
        }

    def run(self, body: str, answers=(), *, login_rc: int = 0, prelude: str = PRELUDE):
        self.answers.write_text("".join(f"{a}\n" for a in answers), encoding="utf-8")
        env = {**self.env(), "FAKE_LOGIN_RC": str(login_rc)}
        return subprocess.run(
            ["bash", "-c", prelude + body],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def login_calls(self) -> list:
        return [line.split(":", 1)[1] for line in self.calls.read_text().splitlines() if line.startswith("login:")]

    def values(self) -> dict:
        # The file alone: read_env() would add this process's environment, where
        # other tests leave a session string.
        return dict(dotenv_values(self.config_path))

    def labels(self) -> list:
        return config.configured_labels(self.values())


@pytest.fixture
def box(tmp_path):
    if not LOGIN_BIN.exists():
        pytest.skip("telegram-mcp-ag-login is not installed next to this interpreter")
    return Sandbox(tmp_path)


def _bash(script: str, **env):
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "TELEGRAM_MCP_AG_REF": "test", "INSTALL_SH": str(INSTALL_SH), **env},
        timeout=60,
    )


# -- flags ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, expected",
    [
        ("", "install|"),
        ("--relogin", "relogin|"),
        ("--relogin --account work", "relogin|work"),
        ("--relogin --account=Work", "relogin|Work"),
        ("--account work --relogin", "relogin|work"),
        ("--add-account", "add-account|"),
        ("--uninstall", "uninstall|"),
    ],
)
def test_flags_are_parsed(args, expected):
    result = _bash(f'source "$INSTALL_SH"; parse_args {args}; echo "$ACTION|$ACCOUNT_ARG"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "args, message",
    [
        ("--account work", "только вместе с --relogin"),
        ("--add-account --account work", "только вместе с --relogin"),
        ("--relogin --account", "нужно название аккаунта"),
        ("--relogin --account --add-account", "нужно название аккаунта"),
        ("--relogin --account ''", "нужно название аккаунта"),
        ("--relogin --account рабочий", "латинские буквы"),
        ("--relogin --account 'a b'", "латинские буквы"),
        ("--relogin --account '$(touch x)'", "латинские буквы"),
        ("--relogin --account " + "a" * 33, "латинские буквы"),
        ("--add-account --relogin", "нельзя сочетать"),
        ("--uninstall --relogin", "нельзя сочетать"),
        ("--bogus", "Неизвестный флаг"),
    ],
)
def test_wrong_flags_are_refused_with_a_reason(args, message):
    result = _bash(f'source "$INSTALL_SH"; parse_args {args}; echo reached')
    assert result.returncode == 1
    assert message in result.stderr
    assert "reached" not in result.stdout


def test_help_lists_the_account_flags():
    result = _bash('source "$INSTALL_SH"; parse_args --help')
    assert result.returncode == 0
    for flag in ("--add-account", "--relogin", "--account", "--uninstall"):
        assert flag in result.stdout


def test_a_refused_account_label_does_not_create_files(tmp_path):
    result = _bash(
        f'cd "{tmp_path}"; source "$INSTALL_SH"; parse_args --relogin --account \'$(touch pwned)\' || true',
    )
    assert not (tmp_path / "pwned").exists(), result.stderr


def test_rerun_commands_match_the_ones_the_server_prints():
    # relogin.py shows these in chat; the installer shows the same ones at the end.
    result = _bash('source "$INSTALL_SH"; installer_command "--relogin --account work"; echo; installer_command "--relogin"')
    mac_account, mac_plain = result.stdout.strip().splitlines()
    assert mac_account == f"{relogin.CMD_MAC_LINUX} --account work"
    assert mac_plain == relogin.CMD_MAC_LINUX


# -- package too old -------------------------------------------------------------


def _old_package(tmp_path: Path) -> Path:
    """A venv whose login command does not know --account."""
    venv = tmp_path / "old-venv"
    (venv / "bin").mkdir(parents=True)
    login = venv / "bin" / "telegram-mcp-ag-login"
    login.write_text("#!/bin/sh\necho 'usage: telegram-mcp-ag-login [--phone]'\n")
    login.chmod(0o755)
    return venv


def test_old_package_cannot_add_an_account_and_changes_nothing(box, tmp_path):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    before = box.config_path.read_bytes()
    old = _old_package(tmp_path)
    result = box.run(f'VENV_DIR="{old}"\nadd_account_flow\n')
    assert result.returncode == 1
    assert "появится в следующей версии" in result.stderr
    assert box.config_path.read_bytes() == before
    assert box.login_calls() == []


def test_old_package_cannot_relogin_by_account_and_changes_nothing(box, tmp_path):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    before = box.config_path.read_bytes()
    old = _old_package(tmp_path)
    result = box.run(f'VENV_DIR="{old}"\nACCOUNT_ARG=work\nrelogin_flow\n')
    assert result.returncode == 1
    assert "появится в следующей версии" in result.stderr
    assert box.config_path.read_bytes() == before


def test_support_check_tells_new_from_old(box, tmp_path):
    old = _old_package(tmp_path)
    assert box.run(f'VENV_DIR="{old}"\nlogin_supports_accounts\n').returncode != 0
    assert box.run("login_supports_accounts\n").returncode == 0


def test_plain_relogin_still_works_with_an_old_package(box, tmp_path):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    old = _old_package(tmp_path)
    result = box.run(f'VENV_DIR="{old}"\nACCOUNT_ARG=""\nrelogin_flow\n')
    assert result.returncode == 0, result.stderr
    assert box.login_calls() == [""]


# -- adding an account -----------------------------------------------------------


def test_adding_an_account_keeps_both_and_sets_the_default(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    # new label, name for the first one, default: 1) personal
    result = box.run("add_account_flow\n", ["work", "personal", "1"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.login_calls() == ["work"]
    assert box.labels() == ["personal", "work"]
    values = box.values()
    assert values["TELEGRAM_SESSION_STRING_PERSONAL"] == "first-session"
    assert values["TELEGRAM_SESSION_STRING_WORK"] == "new-session-for-work"
    assert "TELEGRAM_SESSION_STRING" not in values
    assert config.default_account(values) == "personal"
    assert oct(box.config_path.stat().st_mode & 0o777) == "0o600"
    assert "work" in result.stdout and "перезапустите" in result.stderr.lower()


def test_adding_an_account_can_leave_no_default(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    result = box.run("add_account_flow\n", ["work", "personal", "0"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.default_account(box.values()) is None


def test_enter_on_the_default_question_means_none_when_there_was_none(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    result = box.run("add_account_flow\n", ["work", "personal", ""])
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.default_account(box.values()) is None


def test_enter_names_the_first_account_personal(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    result = box.run("add_account_flow\n", ["work", "", "0"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.labels() == ["personal", "work"]
    assert box.values()["TELEGRAM_SESSION_STRING_PERSONAL"] == "first-session"
    assert "TELEGRAM_SESSION_STRING" not in box.values()


def test_when_the_new_account_is_personal_enter_leaves_the_first_one_unnamed(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    result = box.run("add_account_flow\n", ["personal", "", "0"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.labels() == ["default", "personal"]
    assert box.values()["TELEGRAM_SESSION_STRING"] == "first-session"


def test_an_existing_default_follows_the_rename_and_enter_keeps_it(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"}, extra="TELEGRAM_DEFAULT_ACCOUNT=default\n")
    result = box.run("add_account_flow\n", ["work", "personal", ""])
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.default_account(box.values()) == "personal"


def test_a_third_account_is_added_next_to_two_named_ones(box):
    box.seed(
        {"TELEGRAM_SESSION_STRING_PERSONAL": "p", "TELEGRAM_SESSION_STRING_WORK": "w"},
        extra="TELEGRAM_DEFAULT_ACCOUNT=work\n",
    )
    result = box.run("add_account_flow\n", ["side", ""])  # Enter: keep "work" as the default
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.labels() == ["personal", "side", "work"]
    assert config.default_account(box.values()) == "work"
    assert box.login_calls() == ["side"]


def test_bad_labels_are_asked_again(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    # Cyrillic, the reserved word, the word that means "no default account",
    # empty, then a good one; the first account's name may not repeat the new
    # one either.
    answers = ["рабочий", "default", "none", "", "Work", "work", "personal", "0"]
    result = box.run("add_account_flow\n", answers)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "латинские буквы" in result.stderr
    assert "зарезервирована" in result.stderr
    assert "служебное значение" in result.stderr
    assert "уже занято" in result.stderr
    assert box.labels() == ["personal", "work"]
    assert box.login_calls() == ["work"]


def test_a_label_that_exists_is_not_taken_twice(box):
    box.seed({"TELEGRAM_SESSION_STRING_WORK": "w"})
    result = box.run("add_account_flow\n", ["WORK", "other", ""])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "уже занят" in result.stderr
    assert box.labels() == ["other", "work"]


def test_a_declined_login_leaves_the_config_untouched(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    before = box.config_path.read_bytes()
    # The person says "wrong account" (exit code 5) and does not want to retry.
    result = box.run("add_account_flow\n", ["work", "personal", "1", "n"], login_rc=5)
    assert result.returncode == 1
    assert "ничего не сохранено" in result.stdout
    assert box.config_path.read_bytes() == before
    assert box.labels() == ["default"]


def test_a_failed_login_leaves_the_config_untouched(box):
    box.seed({"TELEGRAM_SESSION_STRING": "first-session"})
    before = box.config_path.read_bytes()
    result = box.run("add_account_flow\n", ["work", "personal", "1"], login_rc=1)
    assert result.returncode == 1
    assert "--relogin --account work" in result.stderr
    assert box.config_path.read_bytes() == before


def test_adding_needs_a_first_account(box):
    result = box.run("add_account_flow\n")
    assert result.returncode == 1
    assert "Первого аккаунта ещё нет" in result.stderr
    assert not box.config_path.exists()


# -- signing in again --------------------------------------------------------------


def _two_accounts(box):
    box.seed(
        {"TELEGRAM_SESSION_STRING_PERSONAL": "p-old", "TELEGRAM_SESSION_STRING_WORK": "w-old"},
        extra="TELEGRAM_DEFAULT_ACCOUNT=personal\n",
    )


def test_relogin_with_an_account_replaces_only_that_session(box):
    _two_accounts(box)
    result = box.run('ACCOUNT_ARG=work\nrelogin_flow\n')
    assert result.returncode == 0, result.stdout + result.stderr
    values = box.values()
    assert values["TELEGRAM_SESSION_STRING_WORK"] == "new-session-for-work"
    assert values["TELEGRAM_SESSION_STRING_PERSONAL"] == "p-old"
    assert config.default_account(values) == "personal"
    assert box.login_calls() == ["work"]


def test_relogin_accepts_any_spelling_of_the_label(box):
    _two_accounts(box)
    result = box.run('ACCOUNT_ARG=WORK\nrelogin_flow\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.login_calls() == ["work"]


def test_relogin_of_an_unknown_account_names_the_known_ones(box):
    _two_accounts(box)
    before = box.config_path.read_bytes()
    result = box.run('ACCOUNT_ARG=nope\nrelogin_flow\n')
    assert result.returncode == 1
    assert "personal, work" in result.stderr
    assert "--add-account" in result.stderr
    assert box.config_path.read_bytes() == before
    assert box.login_calls() == []


def test_relogin_without_a_label_asks_which_when_there_are_several(box):
    _two_accounts(box)
    result = box.run('ACCOUNT_ARG=""\nrelogin_flow\n', ["x", "2"])  # a wrong answer, then 2) work
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Нужно ввести номер" in result.stderr
    assert box.login_calls() == ["work"]
    assert box.values()["TELEGRAM_SESSION_STRING_PERSONAL"] == "p-old"


def test_relogin_with_one_account_asks_nothing(box):
    box.seed({"TELEGRAM_SESSION_STRING": "old"})
    result = box.run('ACCOUNT_ARG=""\nrelogin_flow\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.login_calls() == [""]


def test_relogin_with_one_named_account_asks_nothing(box):
    box.seed({"TELEGRAM_SESSION_STRING_WORK": "old"})
    result = box.run('ACCOUNT_ARG=""\nrelogin_flow\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.login_calls() == [""]


def test_login_command_asking_for_an_account_is_answered(box):
    # Exit code 4 from the login command: several accounts and none named.
    _two_accounts(box)
    prelude = PRELUDE + r"""
_run_login_once() {
    echo "login:${1:-}" >>"$CALLS"
    [[ -n "${1:-}" ]] || return 4
    return 0
}
"""
    result = box.run('API_ID=1; API_HASH=x\nrun_telegram_login ""\n', ["1"], prelude=prelude)
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.login_calls() == ["", "personal"]


def test_a_declined_relogin_can_be_retried(box):
    _two_accounts(box)
    prelude = PRELUDE + r"""
_run_login_once() {
    echo "login:${1:-}" >>"$CALLS"
    [[ "$(wc -l <"$CALLS")" -ge 2 ]] || return 5
    return 0
}
"""
    result = box.run('API_ID=1; API_HASH=x\nrun_telegram_login work\n', ["y"], prelude=prelude)
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.login_calls() == ["work", "work"]


# -- api_id / api_hash rejected (exit code 3) ---------------------------------------


def test_new_api_credentials_do_not_erase_the_sessions(box):
    box.seed(
        {"TELEGRAM_SESSION_STRING_PERSONAL": "p", "TELEGRAM_SESSION_STRING_WORK": "w"},
        extra="# my note\nTELEGRAM_EXPOSED_TOOLS=read-only\nTELEGRAM_DEFAULT_ACCOUNT=work\n",
    )
    result = box.run('API_ID=777; API_HASH=fedcba9876543210fedcba9876543210\nsave_api_credentials\n')
    assert result.returncode == 0, result.stdout + result.stderr
    text = box.config_path.read_text(encoding="utf-8")
    assert "TELEGRAM_API_ID=777" in text
    assert "TELEGRAM_API_HASH=fedcba9876543210fedcba9876543210" in text
    assert "TELEGRAM_SESSION_STRING_PERSONAL=p" in text
    assert "TELEGRAM_SESSION_STRING_WORK=w" in text
    assert "# my note" in text and "TELEGRAM_DEFAULT_ACCOUNT=work" in text
    assert oct(box.config_path.stat().st_mode & 0o777) == "0o600"
    assert not list(box.config_path.parent.glob("config.env.bak-*"))


def test_api_credentials_without_sessions_are_written_as_before(box):
    box.config_path.parent.mkdir(parents=True)
    box.config_path.write_text("TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=old\n")
    result = box.run('API_ID=777; API_HASH=fedcba9876543210fedcba9876543210\nsave_api_credentials\n')
    assert result.returncode == 0, result.stdout + result.stderr
    text = box.config_path.read_text(encoding="utf-8")
    assert "TELEGRAM_API_ID=777" in text and "TELEGRAM_EXPOSED_TOOLS=read-only" in text
    assert list(box.config_path.parent.glob("config.env.bak-*"))


def test_api_credentials_do_not_appear_in_the_process_list(box):
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("save_api_credentials() {")
    body = text[start : text.index("# ----", start)]
    assert 'TELEGRAM_API_HASH="$API_HASH" "$VENV_DIR/bin/python" -c' in body
    assert "sys.argv" not in body


# -- messages and the self-check ----------------------------------------------------


def test_summary_lists_accounts_and_every_relogin_command(box):
    _two_accounts(box)
    result = box.run("print_summary\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "personal (по умолчанию), work" in result.stdout
    assert "--add-account" in result.stdout
    assert "--relogin --account personal" in result.stdout
    assert "--relogin --account work" in result.stdout


def test_summary_for_one_account_offers_adding_another(box):
    box.seed({"TELEGRAM_SESSION_STRING": "s"})
    result = box.run("print_summary\n")
    assert "--add-account" in result.stdout
    assert "--relogin --account" not in result.stdout
    assert "bash -s -- --relogin" in result.stdout


def test_summary_with_an_old_package_is_as_before(box, tmp_path):
    box.seed({"TELEGRAM_SESSION_STRING": "s"})
    old = _old_package(tmp_path)
    result = box.run(f'VENV_DIR="{old}"\nprint_summary\n')
    assert "--add-account" not in result.stdout
    assert "bash -s -- --relogin" in result.stdout


def test_already_signed_in_message_shows_the_accounts(box):
    _two_accounts(box)
    result = box.run("install_login_flow\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "personal (по умолчанию), work" in result.stdout
    assert "--add-account" in result.stdout
    assert box.login_calls() == []


def test_self_check_names_an_account_only_when_there_are_several(box):
    box.seed({"TELEGRAM_SESSION_STRING": "s"})
    assert box.run("_self_check_account\n").stdout == ""

    _two_accounts(box)
    assert box.run("_self_check_account\n").stdout == "personal"  # the default
    assert box.run("SELF_CHECK_ACCOUNT=work\n_self_check_account\n").stdout == "work"

    box.seed(
        {"TELEGRAM_SESSION_STRING_PERSONAL": "p", "TELEGRAM_SESSION_STRING_WORK": "w"},
    )
    box.config_path.write_text(
        API_LINES + "TELEGRAM_SESSION_STRING_PERSONAL=p\nTELEGRAM_SESSION_STRING_WORK=w\n",
        encoding="utf-8",
    )
    assert box.run("_self_check_account\n").stdout == "personal"  # first, no default


def test_self_check_passes_the_account_to_the_check():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'python" - "$account" <<' in text
    assert "check_transcription_access(account=account)" in text


# -- the real ask() ------------------------------------------------------------
#
# Everything above swaps ask() for a stand-in (see PRELUDE), so none of it could
# notice that the real one handed its answer to nobody: ask() keeps the reply in
# a local variable, and a caller that passed the same name got an empty string.
# Here the real ask() reads from a pipe that gives one line per question.

REAL_ASK_PRELUDE = PRELUDE.replace(
    "ask() { local r=\"\"; IFS= read -r r <&3 || true; printf -v \"$2\" '%s' \"$r\"; }\n", ""
)
assert REAL_ASK_PRELUDE != PRELUDE

# The pipe is held open for reading and writing by this shell (fd 4), so what is
# put into it stays there until ask() reads its line; a writer that came and went
# between two questions would lose the lines it had written.
REAL_ASK_FEED = r"""
FIFO="$HOME/tty"
mkfifo "$FIFO"
exec 4<>"$FIFO"
while IFS= read -r line <&3; do printf '%s\n' "$line" >&4; done
TTY_DEVICE="$FIFO"
"""


def _run_with_real_ask(box, body: str, answers) -> subprocess.CompletedProcess:
    box.home.mkdir(parents=True, exist_ok=True)
    box.answers.write_text("".join(f"{a}\n" for a in answers), encoding="utf-8")
    try:
        return subprocess.run(
            ["bash", "-c", REAL_ASK_PRELUDE + REAL_ASK_FEED + body],
            capture_output=True,
            text=True,
            env=box.env(),
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the question was asked again and again: the answer never reached the caller")


def test_the_real_ask_hands_a_label_to_ask_new_label(box):
    result = _run_with_real_ask(box, 'ask_new_label "Name: " "" 0\necho "label=$ASKED_LABEL"\n', ["work"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "label=work" in result.stdout


def test_the_real_ask_hands_a_number_to_choose_account(box):
    body = 'ACCOUNT_LABELS=$\'personal\\nwork\'\nchoose_account "Which?"\necho "chosen=$CHOSEN_ACCOUNT"\n'
    result = _run_with_real_ask(box, body, ["2"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "chosen=work" in result.stdout


@pytest.mark.parametrize("answer, expected", [("2", "work"), ("0", ""), ("", "personal")])
def test_the_real_ask_hands_a_number_to_choose_default_account(box, answer, expected):
    body = 'choose_default_account $\'personal\\nwork\' "personal"\necho "default=[$CHOSEN_DEFAULT]"\n'
    result = _run_with_real_ask(box, body, [answer])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"default=[{expected}]" in result.stdout


def test_a_label_check_that_crashes_stops_instead_of_asking_forever(box):
    # Exit code 2 means "the label is not allowed, ask again"; a crash of the
    # interpreter (code 1) is not a wrong answer and must end the installer.
    body = '_validated_label() { echo boom >&2; return 1; }\nask_new_label "Name: " "" 0\necho unreachable\n'
    result = _run_with_real_ask(box, body, ["work"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "не запускается" in result.stderr
    assert "unreachable" not in result.stdout


def test_a_refused_label_is_asked_again_with_the_real_check(box):
    body = 'ask_new_label "Name: " "" 0\necho "label=$ASKED_LABEL"\n'
    result = _run_with_real_ask(box, body, ["default", "work"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "зарезервирована" in result.stderr
    assert "label=work" in result.stdout
