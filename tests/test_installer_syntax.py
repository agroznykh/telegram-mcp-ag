"""Static syntax checks for the installers.

These never touch the network or a Telegram account; they just make sure
install.sh / install.ps1 parse cleanly. The linters (shellcheck,
PSScriptAnalyzer) are skipped locally when not installed and run for real
in CI, which has them preinstalled on the relevant OS images.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"


def _powershell_executable():
    return shutil.which("pwsh") or shutil.which("powershell")


def test_install_sh_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_passes_shellcheck():
    result = subprocess.run(["shellcheck", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_powershell_executable() is None, reason="no PowerShell interpreter available")
def test_install_ps1_has_valid_powershell_syntax():
    script = (
        "$parseErrors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$null, [ref]$parseErrors) | Out-Null; "
        "if ($parseErrors.Count -gt 0) { $parseErrors | ForEach-Object { Write-Error $_ }; exit 1 } "
        "else { exit 0 }"
    )
    result = subprocess.run(
        [_powershell_executable(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_powershell_executable() is None, reason="no PowerShell interpreter available")
def test_install_ps1_passes_psscriptanalyzer():
    script = (
        "if (-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)) { exit 2 }; "
        f"$results = Invoke-ScriptAnalyzer -Path '{INSTALL_PS1}' -Severity Error; "
        "if ($results) { $results | Format-Table | Out-String | Write-Output; exit 1 } else { exit 0 }"
    )
    result = subprocess.run(
        [_powershell_executable(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )
    if result.returncode == 2:
        pytest.skip("PSScriptAnalyzer module not installed")
    assert result.returncode == 0, result.stdout + result.stderr
