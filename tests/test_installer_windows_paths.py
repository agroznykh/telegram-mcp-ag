"""Where install.ps1 looks for Claude Desktop's config and for Claude Code.

Runs the real PowerShell functions (install.ps1 can be dot-sourced: it skips
Invoke-Main then) against throwaway directory trees that mimic the three ways
Claude Desktop lays out its data on Windows. No network, no Telegram.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "install.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(PWSH is None, reason="no PowerShell interpreter available")


def _run(tmp_path: Path, command: str, *, path_dirs: str = "") -> str:
    """Dot-source install.ps1 in a sandboxed profile and return the command's output."""
    assert PWSH is not None
    home = tmp_path / "home"
    env = {
        **os.environ,
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        # A real `claude` on the developer's PATH must not leak into the result.
        "PATH": path_dirs or str(tmp_path / "empty-path"),
    }
    # PWSH is a full path, so PATH is free to be empty.
    script = f". '{INSTALL_PS1}'; {command}"
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _touch(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _desktop_dir(tmp_path: Path) -> str:
    return _run(tmp_path, "Get-ClaudeDesktopDir")


def test_desktop_dir_is_none_when_nothing_is_installed(tmp_path):
    assert _desktop_dir(tmp_path) == ""


def test_desktop_dir_finds_the_classic_roaming_folder(tmp_path):
    (tmp_path / "roaming" / "Claude").mkdir(parents=True)
    assert Path(_desktop_dir(tmp_path)) == tmp_path / "roaming" / "Claude"


def test_desktop_dir_finds_the_store_data_folder_outside_the_package(tmp_path):
    data = tmp_path / "local" / "Claude-Data" / "claude_pzs8sxrjxfjjc"
    data.mkdir(parents=True)
    assert Path(_desktop_dir(tmp_path)) == data


def test_desktop_dir_does_not_depend_on_the_publisher_id(tmp_path):
    data = tmp_path / "local" / "Claude-Data" / "claude_someotherid"
    data.mkdir(parents=True)
    assert Path(_desktop_dir(tmp_path)) == data


def test_desktop_dir_finds_the_virtualized_folder_of_an_older_store_build(tmp_path):
    virtual = tmp_path / "local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    virtual.mkdir(parents=True)
    assert Path(_desktop_dir(tmp_path)) == virtual


def test_desktop_dir_skips_a_package_folder_the_app_has_moved_its_data_out_of(tmp_path):
    # What a current Store build leaves behind in the old location.
    virtual = tmp_path / "local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    _touch(virtual / ".user-data-left.claude_pzs8sxrjxfjjc")
    data = tmp_path / "local" / "Claude-Data" / "claude_pzs8sxrjxfjjc"
    data.mkdir(parents=True)
    assert Path(_desktop_dir(tmp_path)) == data


def test_desktop_dir_ignores_a_moved_out_package_folder_when_nothing_else_exists(tmp_path):
    virtual = tmp_path / "local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    _touch(virtual / ".user-data-left.claude_pzs8sxrjxfjjc")
    assert _desktop_dir(tmp_path) == ""


def test_desktop_dir_prefers_the_store_data_folder_over_the_classic_one(tmp_path):
    (tmp_path / "roaming" / "Claude").mkdir(parents=True)
    data = tmp_path / "local" / "Claude-Data" / "claude_pzs8sxrjxfjjc"
    data.mkdir(parents=True)
    assert Path(_desktop_dir(tmp_path)) == data


def test_store_install_is_detected_even_without_a_data_folder(tmp_path):
    assert _run(tmp_path, "Test-ClaudeDesktopStoreInstalled") == "False"
    (tmp_path / "local" / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    assert _run(tmp_path, "Test-ClaudeDesktopStoreInstalled") == "True"


def test_claude_code_is_none_when_not_installed(tmp_path):
    assert _run(tmp_path, "Get-ClaudeCodeExe") == ""


def test_claude_code_is_found_in_the_desktop_apps_private_copy(tmp_path):
    exe = _touch(
        tmp_path / "local" / "Claude-Data" / "claude_pzs8sxrjxfjjc" / "claude-code" / "2.1.317" / "a33aa5518497" / "claude.exe"
    )
    assert Path(_run(tmp_path, "Get-ClaudeCodeExe")) == exe


def test_claude_code_prefers_the_newest_private_copy(tmp_path):
    base = tmp_path / "local" / "Claude-Data" / "claude_pzs8sxrjxfjjc" / "claude-code"
    old = _touch(base / "2.1.300" / "aaaaaaaaaaaa" / "claude.exe")
    new = _touch(base / "2.1.317" / "bbbbbbbbbbbb" / "claude.exe")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (2_000_000_000, 2_000_000_000))
    assert Path(_run(tmp_path, "Get-ClaudeCodeExe")) == new


def test_claude_on_path_wins_over_the_private_copy(tmp_path):
    _touch(tmp_path / "local" / "Claude-Data" / "claude_pzs8sxrjxfjjc" / "claude-code" / "2.1.317" / "h" / "claude.exe")
    bin_dir = tmp_path / "bin"
    # Windows resolves `claude` to claude.exe through PATHEXT; elsewhere the
    # file must be named exactly `claude` and be executable.
    on_path = _touch(bin_dir / ("claude.exe" if os.name == "nt" else "claude"))
    on_path.chmod(0o755)
    found = _run(tmp_path, "Get-ClaudeCodeExe", path_dirs=str(bin_dir))
    assert Path(found) == on_path


def test_upsert_keeps_existing_settings_and_adds_the_server(tmp_path):
    # The script the installer feeds to Python must leave the app's own keys alone.
    config = _touch(
        tmp_path / "claude_desktop_config.json",
        json.dumps({"coworkUserFilesPath": "D:\\Новая папка", "preferences": {"sidebarMode": "x"}}, ensure_ascii=False),
    )
    out = _run(tmp_path, "$script:ClaudeDesktopJsonUpsertScript")
    script = tmp_path / "upsert.py"
    script.write_text(out, encoding="utf-8")
    subprocess.run(["python3" if os.name != "nt" else "python", "-I", str(script), str(config), "C:\\x\\server.exe", "telegram-mcp-ag"], check=True)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["coworkUserFilesPath"] == "D:\\Новая папка"
    assert data["preferences"] == {"sidebarMode": "x"}
    assert data["mcpServers"]["telegram-mcp-ag"] == {"command": "C:\\x\\server.exe", "args": []}


def test_rerun_command_works_without_a_local_install_ps1(tmp_path):
    # The installer runs from a temp file, so there is no local install.ps1 to point at.
    command = _run(tmp_path, "Get-InstallerCommand '-Relogin'")
    assert command.endswith("-ExecutionPolicy Bypass -File $f -Relogin")
    assert "iwr 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1'" in command
