"""Static syntax checks for the installers.

These never touch the network or a Telegram account; they just make sure
install.sh / install.ps1 parse cleanly. The linters (shellcheck,
PSScriptAnalyzer) are skipped locally when not installed and run for real
in CI, which has them preinstalled on the relevant OS images.

install.sh targets macOS/Linux, so its checks are skipped on Windows: the
"bash" on the Windows runner's PATH is the WSL launcher stub, not a real
shell, and fails with "no installed distributions" rather than anything
about the script.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

not_windows = pytest.mark.skipif(sys.platform == "win32", reason="install.sh targets macOS/Linux")


def _powershell_executable():
    return shutil.which("pwsh") or shutil.which("powershell")


def test_installers_can_read_the_upstream_sha_from_pyproject():
    # Both installers download upstream by the SHA they grep out of this line.
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"chigwell/telegram-mcp\.git@[0-9a-f]{40}", pyproject)


def _run_bash_migration(config_path: Path):
    script = f'source "{INSTALL_SH}"; CONFIG_PATH="{config_path}"; enable_media_download'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@not_windows
@pytest.mark.parametrize(
    "line",
    [
        "TELEGRAM_EXPOSED_TOOLS=read-only",
        '  TELEGRAM_EXPOSED_TOOLS = "read-only" ',
        "TELEGRAM_EXPOSED_TOOLS='read-only'",
    ],
)
def test_install_sh_switches_plain_read_only_to_download_mode(tmp_path, line):
    config_path = tmp_path / "config.env"
    config_path.write_text(f"A=1\n{line}\nB=2\n", encoding="utf-8")
    config_path.chmod(0o644)

    result = _run_bash_migration(config_path)

    assert result.returncode == 0, result.stderr
    assert config_path.read_text(encoding="utf-8") == (
        "A=1\nTELEGRAM_EXPOSED_TOOLS=read-only+download_media\nB=2\n"
    )
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert "Включено скачивание" in result.stdout + result.stderr


@not_windows
@pytest.mark.parametrize(
    "line",
    ["TELEGRAM_EXPOSED_TOOLS=all", "TELEGRAM_EXPOSED_TOOLS=read-only+send_message", "TELEGRAM_EXPOSED_TOOLS="],
)
def test_install_sh_leaves_any_other_mode_alone(tmp_path, line):
    config_path = tmp_path / "config.env"
    config_path.write_text(f"A=1\n{line}\n", encoding="utf-8")

    result = _run_bash_migration(config_path)

    assert result.returncode == 0, result.stderr
    assert config_path.read_text(encoding="utf-8") == f"A=1\n{line}\n"
    assert not list(tmp_path.glob("config.env.*"))


@not_windows
def test_install_sh_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@not_windows
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
