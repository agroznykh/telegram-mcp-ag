"""Account handling in install.ps1, the counterpart of test_installer_accounts.py.

install.ps1 can be dot-sourced (it then defines everything and skips Invoke-Main),
so these run the real PowerShell functions. The commands that need Telegram or the
installed package are replaced by stand-ins defined after the dot-source. Messages
are Russian and the console encoding differs between PowerShell versions, so the
assertions look at exit codes and at the ASCII parts of the messages. No network,
no Telegram.

Two tests call the package's Python (through this interpreter standing in for the
venv's).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from telegram_mcp_ag import relogin

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "install.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(PWSH is None, reason="no PowerShell interpreter available")


def _run(tmp_path: Path, command: str, *, check: bool = True):
    """Dot-source install.ps1 in a sandboxed profile and run ``command``."""
    assert PWSH is not None
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "USERPROFILE": str(home),
        "HOME": str(home),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        # Without it the installer asks GitHub for the latest release.
        "TELEGRAM_MCP_AG_REF": "test",
    }
    script = f". '{INSTALL_PS1}'; {command}"
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_rerun_commands_match_the_ones_the_server_prints(tmp_path):
    out = _run(tmp_path, "Get-InstallerCommand '-Relogin -Account work'; Get-InstallerCommand '-Relogin'").stdout
    with_account, plain = [line.strip() for line in out.strip().splitlines()]
    assert with_account == f"{relogin.CMD_WINDOWS} -Account work"
    assert plain == relogin.CMD_WINDOWS


@pytest.mark.parametrize(
    "arguments",
    [
        "-Account work",
        "-AddAccount -Account work",
        "-Relogin -Account 'a b'",
        "-Relogin -Account ('a' * 33)",
        "-AddAccount -Relogin",
        "-Uninstall -Relogin",
        "-Uninstall -AddAccount",
    ],
)
def test_wrong_flag_combinations_are_refused(tmp_path, arguments):
    result = _run(tmp_path, f"Assert-InstallerArguments {arguments}; Write-Output reached", check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "reached" not in result.stdout


@pytest.mark.parametrize(
    "arguments",
    ["", "-Relogin", "-Relogin -Account work", "-Relogin -Account Work_1", "-AddAccount", "-Uninstall"],
)
def test_right_flag_combinations_pass(tmp_path, arguments):
    result = _run(tmp_path, f"Assert-InstallerArguments {arguments}; Write-Output reached")
    assert "reached" in result.stdout


def test_account_list_is_read_from_the_login_command(tmp_path):
    command = r"""
function Get-LoginCliOutput { param([string[]]$Arguments) return @{ Status = 0; Output = "personal (x)`nwork`n`nBAD`nwe!rd (y)`nside_1`n" } }
if (-not (Read-AccountList)) { throw 'not read' }
Write-Output ($script:AccountLabels -join ',')
Write-Output $script:AccountDefault
Write-Output (Format-Accounts)
"""
    out = _run(tmp_path, command).stdout.splitlines()
    assert out[0] == "personal,work,side_1"
    assert out[1] == "personal"
    assert out[2].startswith("personal (") and out[2].endswith("), work, side_1")


def test_account_list_failure_is_reported(tmp_path):
    command = r"""
function Get-LoginCliOutput { param([string[]]$Arguments) return @{ Status = 2; Output = '' } }
Write-Output (Read-AccountList)
Write-Output $script:AccountLabels.Count
"""
    assert _run(tmp_path, command).stdout.split() == ["False", "0"]


def test_support_check_tells_new_from_old(tmp_path):
    command = r"""
function Get-LoginCliOutput { param([string[]]$Arguments) return @{ Status = 0; Output = 'usage: login [--phone]' } }
Write-Output (Test-LoginSupportsAccounts)
function Get-LoginCliOutput { param([string[]]$Arguments) return @{ Status = 0; Output = 'usage: login [--phone] [--account X]' } }
Write-Output (Test-LoginSupportsAccounts)
"""
    assert _run(tmp_path, command).stdout.split() == ["False", "True"]


def test_old_package_cannot_add_an_account(tmp_path):
    command = r"""
function Get-LoginCliOutput { param([string[]]$Arguments) return @{ Status = 0; Output = 'usage: login [--phone]' } }
Invoke-AddAccount
Write-Output reached
"""
    result = _run(tmp_path, command, check=False)
    assert result.returncode == 1
    assert "reached" not in result.stdout


def test_old_package_cannot_relogin_by_account(tmp_path):
    command = r"""
function Get-LoginCliOutput { param([string[]]$Arguments) return @{ Status = 0; Output = 'usage: login [--phone]' } }
Invoke-ReloginAccount -Account work
Write-Output reached
"""
    result = _run(tmp_path, command, check=False)
    assert result.returncode == 1
    assert "reached" not in result.stdout


def test_a_declined_login_can_be_retried_with_the_same_account(tmp_path):
    command = r"""
$script:Statuses = @(5, 0)
$script:Calls = @()
function Invoke-LoginProcess {
    param([string]$ApiId, [string]$ApiHash, [string[]]$Arguments)
    $script:Calls += ($Arguments -join ' ')
    $script:LoginExitCode = $script:Statuses[$script:Calls.Count - 1]
}
function Confirm-Action { param([string]$Prompt) return $true }
Invoke-TelegramLogin -ApiId 1 -ApiHash x -Account work -Qr
Write-Output ($script:Calls -join '|')
"""
    assert _run(tmp_path, command).stdout.strip().splitlines()[-1] == "--account work|--account work"


def test_a_declined_login_that_is_not_retried_fails(tmp_path):
    command = r"""
function Invoke-LoginProcess { param([string]$ApiId, [string]$ApiHash, [string[]]$Arguments) $script:LoginExitCode = 5 }
function Confirm-Action { param([string]$Prompt) return $false }
Invoke-TelegramLogin -ApiId 1 -ApiHash x -Account work -Qr
Write-Output reached
"""
    result = _run(tmp_path, command, check=False)
    assert result.returncode == 1
    assert "reached" not in result.stdout
    assert "-Relogin -Account work" in result.stdout


def test_the_login_command_asking_for_an_account_is_answered(tmp_path):
    command = r"""
$script:Calls = @()
function Invoke-LoginProcess {
    param([string]$ApiId, [string]$ApiHash, [string[]]$Arguments)
    $script:Calls += ('[' + ($Arguments -join ' ') + ']')
    $script:LoginExitCode = 0
    if ($Arguments -notcontains '--account') { $script:LoginExitCode = 4 }
}
function Read-AccountList { $script:AccountLabels = @('personal', 'work'); return $true }
function Read-AccountChoice { param([string]$Question) return 'work' }
Invoke-TelegramLogin -ApiId 1 -ApiHash x -Qr
Write-Output ($script:Calls -join '|')
"""
    assert _run(tmp_path, command).stdout.strip().splitlines()[-1] == "[]|[--account work]"


def test_rejected_api_credentials_are_asked_again_and_saved_without_touching_sessions(tmp_path):
    command = r"""
$script:Statuses = @(3, 0)
$script:Calls = @()
$script:Saved = @()
function Invoke-LoginProcess {
    param([string]$ApiId, [string]$ApiHash, [string[]]$Arguments)
    $script:Calls += "$ApiId"
    $script:LoginExitCode = $script:Statuses[$script:Calls.Count - 1]
}
function Read-ApiCredentials { return @{ ApiId = '777'; ApiHash = 'new' } }
function Save-ApiCredentials { param([string]$ApiId, [string]$ApiHash) $script:Saved += "$ApiId/$ApiHash" }
function Write-ConfigEnv { throw 'must not rewrite config.env from scratch' }
Invoke-TelegramLogin -ApiId 1 -ApiHash old -Account work -Qr
Write-Output (($script:Calls -join ',') + ' ' + ($script:Saved -join ','))
"""
    assert _run(tmp_path, command).stdout.strip().splitlines()[-1] == "1,777 777/new"


def test_self_check_names_an_account_only_when_there_are_several(tmp_path):
    command = r"""
function Get-LoginCliOutput { param([string[]]$Arguments)
    if ($Arguments -contains '--help') { return @{ Status = 0; Output = 'usage: login [--account X]' } }
    return @{ Status = 0; Output = $script:Listing }
}
$script:Listing = "only`n"
Write-Output ('[' + (Get-SelfCheckAccount) + ']')
$script:Listing = "personal`nwork (x)`n"
Write-Output ('[' + (Get-SelfCheckAccount) + ']')
$script:Listing = "personal`nwork`n"
Write-Output ('[' + (Get-SelfCheckAccount) + ']')
$script:SelfCheckAccount = 'side'
Write-Output ('[' + (Get-SelfCheckAccount) + ']')
"""
    assert _run(tmp_path, command).stdout.split() == ["[]", "[work]", "[personal]", "[side]"]


def _use_this_interpreter() -> str:
    # install.ps1 runs $VenvDir\Scripts\python.exe; the interpreter of this run
    # (where the package is installed) stands in for it. Not a venv layout on CI.
    return f"function Get-VenvPython {{ '{sys.executable}' }}"


def test_new_api_credentials_do_not_erase_the_sessions(tmp_path):
    config_path = tmp_path / "home" / "telegram-mcp-ag" / "config.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=old\n# note\n"
        "TELEGRAM_SESSION_STRING_PERSONAL=p\nTELEGRAM_SESSION_STRING_WORK=w\n",
        encoding="utf-8",
    )
    _run(tmp_path, f"{_use_this_interpreter()}; Save-ApiCredentials -ApiId 777 -ApiHash newhash")
    text = config_path.read_text(encoding="utf-8")
    assert "TELEGRAM_API_ID=777" in text and "TELEGRAM_API_HASH=newhash" in text
    assert "TELEGRAM_SESSION_STRING_PERSONAL=p" in text and "TELEGRAM_SESSION_STRING_WORK=w" in text
    assert "# note" in text


def test_labels_are_checked_by_the_package(tmp_path):
    command = f"""{_use_this_interpreter()}
Write-Output ('[' + (Get-ValidatedLabel -Label 'Work-1' -AllowDefault $false) + ']')
Write-Output ('[' + (Get-ValidatedLabel -Label 'default' -AllowDefault $false) + ']')
Write-Output ('[' + (Get-ValidatedLabel -Label 'default' -AllowDefault $true) + ']')
"""
    assert _run(tmp_path, command).stdout.split() == ["[work_1]", "[]", "[default]"]


def test_the_word_that_means_no_default_account_is_not_a_label(tmp_path):
    command = r"""
$script:Answers = @('none', 'work')
$script:Index = 0
function Read-Answer { param([string]$Prompt) $answer = $script:Answers[$script:Index]; $script:Index++; return $answer }
function Get-ValidatedLabel { param([string]$Label, [bool]$AllowDefault) return $Label }
Write-Output (Read-NewLabel -Prompt 'x')
"""
    assert _run(tmp_path, command).stdout.strip().splitlines()[-1] == "work"


@pytest.mark.parametrize("code, reached", [(2, True), (1, False)])
def test_a_crashing_label_check_stops_but_a_refusal_does_not(tmp_path, code, reached):
    # Exit code 2 of the check script is "the label is not allowed"; any other
    # failure is the interpreter breaking, and asking again would never end.
    command = f"""{_use_this_interpreter()}
function Invoke-PythonScript {{ param([string]$PythonExe, [string]$ScriptBody, [string[]]$Arguments) & '{PWSH}' -NoProfile -Command 'exit {code}' }}
$null = Get-ValidatedLabel -Label x -AllowDefault $false
Write-Output reached
"""
    result = _run(tmp_path, command, check=False)
    assert ("reached" in result.stdout) is reached, result.stdout + result.stderr
    assert result.returncode == (0 if reached else 1)
