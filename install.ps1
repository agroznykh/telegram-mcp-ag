#Requires -Version 5.1
<#
.SYNOPSIS
    Installer for telegram-mcp-ag (Windows).

.DESCRIPTION
    Finds or bootstraps Python 3.10+, creates a venv, installs the package,
    logs into Telegram, writes config.env, and registers the server with
    whichever of Claude Code / Codex CLI / ChatGPT Desktop / Claude Desktop
    are present. Ports install.sh (macOS/Linux) -- see that file for the
    shared design notes.

    All user-facing text is Russian: this installer targets people who have
    never opened a terminal before (see CLAUDE.md). Code comments are English.

.PARAMETER Relogin
    Redo the Telegram login (e.g. after the session was revoked manually in
    Settings -> Devices), even if config.env already exists.

.PARAMETER Uninstall
    Remove client registrations and offer to delete the install directory.

.PARAMETER Qr
    Force QR-code login (skips the Windows Terminal auto-detection).

.PARAMETER Phone
    Force phone-number login (skips QR entirely).

.EXAMPLE
    iex (irm 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1')

.EXAMPLE
    # For flags, download first -- iex can't forward parameters:
    iwr 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1' -OutFile install.ps1
    .\install.ps1 -Relogin
#>
[CmdletBinding()]
param(
    [switch]$Relogin,
    [switch]$Uninstall,
    [switch]$Qr,
    [switch]$Phone
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # Best effort -- a handful of terminals (old cmd.exe hosts) reject this.
    Write-Verbose "Could not switch console encoding to UTF-8: $($_.Exception.Message)"
}

$RepoUrl = 'https://github.com/agroznykh/telegram-mcp-ag.git'
$RepoApi = 'https://api.github.com/repos/agroznykh/telegram-mcp-ag/releases/latest'

# Installs the latest tagged release by default, falling back to `main` if no
# release exists yet or the GitHub API call fails (offline, rate-limited).
# Override for testing: $env:TELEGRAM_MCP_AG_REF = 'some-branch'
function Resolve-RepoRef {
    if ($env:TELEGRAM_MCP_AG_REF) { return $env:TELEGRAM_MCP_AG_REF }
    try {
        $release = Invoke-RestMethod -Uri $RepoApi -ErrorAction Stop
        if ($release.tag_name) { return $release.tag_name }
    } catch {
        # No release yet, or the API call failed -- main is a fine fallback.
        Write-Verbose "Could not resolve the latest release, using main: $($_.Exception.Message)"
    }
    return 'main'
}
$RepoRef = Resolve-RepoRef

# Must match telegram_mcp_ag.config.CONFIG_DIR exactly -- it is not
# configurable, so this path cannot be overridden here either. Python's
# Path.home() on Windows resolves via USERPROFILE, same as this.
$InstallDir = Join-Path $env:USERPROFILE 'telegram-mcp-ag'
$VenvDir = Join-Path $InstallDir '.venv'
# Initialize-PythonAndVenv renames $VenvDir here before rebuilding it fresh
# in place (not in a staging dir moved into place afterward -- venvs bake an
# absolute path to their own creation location into every console-script
# .exe shim, so renaming a *completed* venv directory breaks all of them).
# See Restore-VenvBackupOnFailure/Confirm-NewVenv.
$VenvBackupDir = Join-Path $InstallDir '.venv.bak'
$ConfigPath = Join-Path $InstallDir 'config.env'
$ServerName = 'telegram-mcp-ag'

$script:MaintenanceVenvs = New-Object System.Collections.Generic.List[string]
# Set by Request-AutoApprove(), read by Register-ClaudeCode()/Register-Codex().
$script:AutoApproveTools = $false

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

function Write-Info { param([string]$Message) Write-Host "[i] $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "[v] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Failure { param([string]$Message) Write-Host "[x] $Message" -ForegroundColor Red }

function Read-Answer {
    param([string]$Prompt)
    return Read-Host -Prompt $Prompt
}

function Confirm-Action {
    param([string]$Prompt)
    $reply = Read-Host -Prompt "$Prompt [y/N]"
    return $reply -match '^[YyДд]$'
}

function Remove-MaintenanceVenvs {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal cleanup helper, not a public cmdlet.')]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '', Justification = 'Removes a collection of throwaway venvs; plural is accurate.')]
    param()
    foreach ($dir in $script:MaintenanceVenvs) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

function Test-SupportedPlatform {
    # $PSVersionTable.Platform only exists on PS6+; its absence on 5.1 means
    # "definitely Windows" (5.1 never shipped anywhere else).
    $platform = $null
    if ($PSVersionTable.ContainsKey('Platform')) {
        $platform = $PSVersionTable.Platform
    }
    if ($platform -and $platform -ne 'Win32NT') {
        Write-Failure 'Этот установщик поддерживает только Windows. Для macOS/Linux используйте install.sh.'
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Python / venv setup (uv is an accelerator, not a requirement -- system
# Python is always preferred when it already satisfies the version floor).
# ---------------------------------------------------------------------------

function Test-PythonCommand {
    param([string]$Exe, [string]$VersionArg)
    try {
        if ($VersionArg) {
            & $Exe $VersionArg -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>$null 1>$null
        } else {
            & $Exe -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>$null 1>$null
        }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-SystemPython {
    # Returns @(exe) or @(exe, versionArg), or $null if nothing suitable
    # was found. The `py` launcher (bundled with the official installer) is
    # preferred since it can target a specific version even when several
    # are installed.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @('-3.13', '-3.12', '-3.11', '-3.10')) {
            if (Test-PythonCommand -Exe 'py' -VersionArg $v) {
                return @('py', $v)
            }
        }
    }
    foreach ($exe in @('python3', 'python')) {
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            if (Test-PythonCommand -Exe $exe -VersionArg $null) {
                return @($exe)
            }
        }
    }
    return $null
}

function Invoke-PythonCommand {
    # Runs a system-Python command found by Find-SystemPython/Find-Uv-style
    # helpers, splatting the optional version-selector argument in front of
    # the caller's own arguments.
    param([string[]]$PyCmd, [string[]]$Arguments)
    $allArgs = @()
    if ($PyCmd.Length -gt 1) { $allArgs += $PyCmd[1] }
    $allArgs += $Arguments
    & $PyCmd[0] @allArgs
}

function Install-Uv {
    Write-Info 'Устанавливаю uv (в %USERPROFILE%\.local\bin)...'
    try {
        powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        Write-Ok 'uv установлен.'
    } catch {
        Write-Warn "Не удалось установить uv: $($_.Exception.Message)"
    }
}

function Initialize-PythonAndVenv {
    $pyCmd = Find-SystemPython

    if (-not $pyCmd -and -not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Warn 'Не нашёл на этой машине Python версии 3.10 или новее.'
        if (Confirm-Action 'Установить менеджер uv -- он сам скачает подходящий Python?') {
            Install-Uv
        }
    }

    if (-not $pyCmd -and -not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Failure 'Нужен Python 3.10+. Установите его с https://www.python.org/downloads/ и запустите установщик ещё раз.'
        exit 1
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Set-Location $InstallDir

    # Rename the old venv aside rather than deleting it: if a later step
    # fails (network hiccup on the pip install below, most commonly),
    # Restore-VenvBackupOnFailure (called from the top-level `finally`) puts
    # it right back, so a working install is never left half-destroyed.
    # Built fresh at $VenvDir itself, not a staging path swapped in
    # afterward -- see $VenvBackupDir's definition for why.
    if (Test-Path $VenvBackupDir) {
        Remove-Item -Recurse -Force $VenvBackupDir
    }
    if (Test-Path $VenvDir) {
        Move-Item -Path $VenvDir -Destination $VenvBackupDir
    }

    if ($pyCmd) {
        Write-Info "Создаю виртуальное окружение ($($pyCmd -join ' '))..."
        Invoke-PythonCommand -PyCmd $pyCmd -Arguments @('-m', 'venv', $VenvDir)
        if ($LASTEXITCODE -ne 0) {
            Write-Failure 'Не удалось создать виртуальное окружение.'
            exit 1
        }
    } else {
        Write-Info 'Создаю виртуальное окружение через uv (при необходимости скачает Python 3.12)...'
        uv venv --seed --python 3.12 $VenvDir | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Failure 'Не удалось создать виртуальное окружение через uv.'
            exit 1
        }
    }
    Write-Ok "Окружение готово: $VenvDir"
}

function Get-VenvPython { Join-Path $VenvDir 'Scripts\python.exe' }
function Get-VenvServerExe { Join-Path $VenvDir 'Scripts\telegram-mcp-ag.exe' }
function Get-VenvGenerateSessionExe { Join-Path $VenvDir 'Scripts\telegram-mcp-generate-session.exe' }

function Install-ServerPackage {
    Write-Info "Устанавливаю telegram-mcp-ag (ref: $RepoRef)..."
    $py = Get-VenvPython
    & $py -m pip install --upgrade pip -q
    & $py -m pip install -q "git+$RepoUrl@$RepoRef"
    if ($LASTEXITCODE -ne 0) {
        Write-Failure 'Не удалось установить пакет.'
        exit 1
    }
    Write-Ok 'Пакет установлен.'
}

# The venv rebuild is confirmed good at this point (Initialize-PythonAndVenv
# and Install-ServerPackage both already succeeded) -- the backup is no
# longer needed, and dropping it here is what makes
# Restore-VenvBackupOnFailure a no-op on a successful run.
function Confirm-NewVenv {
    if (Test-Path $VenvBackupDir) {
        Remove-Item -Recurse -Force $VenvBackupDir
    }
}

# Called from the top-level `finally` (runs on every exit, success or
# failure) -- a backup still present means the rebuild was interrupted
# before Confirm-NewVenv ran, so put the old, working venv back rather than
# leave the user with neither a working old one nor a finished new one.
function Restore-VenvBackupOnFailure {
    if (Test-Path $VenvBackupDir) {
        if (Test-Path $VenvDir) {
            Remove-Item -Recurse -Force $VenvDir
        }
        Move-Item -Path $VenvBackupDir -Destination $VenvDir
    }
}

# ---------------------------------------------------------------------------
# Telegram credentials + login
# ---------------------------------------------------------------------------

function Read-ApiCredentials {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '', Justification = 'Returns both api_id and api_hash; plural is accurate.')]
    param()
    Write-Host ''
    Write-Info 'Понадобятся api_id и api_hash с https://my.telegram.org/apps'
    Write-Info '(войдите под своим номером телефона, откройте "API development tools").'
    Write-Host ''

    $apiId = ''
    while ($true) {
        $apiId = Read-Answer 'api_id'
        if ($apiId -match '^[0-9]+$') { break }
        Write-Warn 'api_id -- это число, попробуйте ещё раз.'
    }

    $apiHash = ''
    while ($true) {
        $apiHash = Read-Answer 'api_hash'
        if ([string]::IsNullOrEmpty($apiHash)) {
            Write-Warn 'api_hash не может быть пустым.'
            continue
        }
        if ($apiHash -notmatch '^[0-9a-fA-F]{32}$') {
            Write-Warn 'Обычно api_hash -- это 32 шестнадцатеричных символа. Введённое значение выглядит иначе.'
            if (Confirm-Action 'Использовать его как есть?') { break }
            continue
        }
        break
    }

    return @{ ApiId = $apiId; ApiHash = $apiHash }
}

function Request-AutoApprove {
    Write-Host ''
    Write-Info 'Сервер умеет только читать Telegram -- отправка сообщений и другие'
    Write-Info 'изменяющие действия физически недоступны, независимо от ответа ниже.'
    if (Confirm-Action 'Разрешать инструменты чтения автоматически, без подтверждения в чате на каждый вызов?') {
        return $true
    }
    Write-Info 'Ассистент будет спрашивать подтверждение на каждый вызов -- это можно изменить позже в настройках клиента.'
    return $false
}

function Invoke-TelegramLogin {
    param([string]$ApiId, [string]$ApiHash, [switch]$Qr, [switch]$Phone)

    Write-Host ''
    Write-Info 'Вход в Telegram.'
    Write-Info 'В приложении Telegram: Настройки -> Устройства -> Подключить устройство -> отсканировать QR-код.'
    Write-Warn 'На вопрос генератора сессии "Would you like to automatically update your .env file? (y/N)" в конце ответьте n -- этот установщик сам запишет config.env.'
    Write-Host ''

    $useQr = $true
    if ($Phone) {
        $useQr = $false
    } elseif ($Qr) {
        $useQr = $true
    } elseif (-not $env:WT_SESSION) {
        # Legacy conhost (not Windows Terminal) frequently mangles the QR
        # code's block characters.
        Write-Warn 'Похоже, вы не в Windows Terminal -- QR-код в старой консоли часто отображается криво.'
        Write-Info 'Использую вход по номеру телефона. Чтобы всё равно попробовать QR: install.ps1 -Qr'
        $useQr = $false
    }

    $env:TELEGRAM_API_ID = $ApiId
    $env:TELEGRAM_API_HASH = $ApiHash
    $env:PYTHONUNBUFFERED = '1'

    $logFile = [System.IO.Path]::GetTempFileName()
    $exe = Get-VenvGenerateSessionExe
    $loginOk = $false

    if ($useQr) {
        & $exe --qr 2>&1 | Tee-Object -FilePath $logFile
        $loginOk = ($LASTEXITCODE -eq 0)
        if (-not $loginOk) {
            Write-Warn 'Вход по QR-коду не удался, пробую вход по номеру телефона.'
        }
    }

    if (-not $loginOk) {
        Set-Content -Path $logFile -Value $null
        & $exe --phone 2>&1 | Tee-Object -FilePath $logFile
        $loginOk = ($LASTEXITCODE -eq 0)
    }

    Remove-Item Env:\TELEGRAM_API_ID, Env:\TELEGRAM_API_HASH, Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue

    # A stray plaintext .env may appear here if the login script's own
    # "update .env?" prompt was answered y. We already parse the session
    # string from the transcript below, so this file is redundant and not
    # locked down the way config.env is -- remove it.
    $strayEnv = Join-Path $InstallDir '.env'
    if (Test-Path $strayEnv) { Remove-Item -Force $strayEnv }

    if (-not $loginOk) {
        Remove-Item -Force $logFile -ErrorAction SilentlyContinue
        Write-Failure 'Не удалось войти в Telegram. Проверьте api_id/api_hash и запустите: .\install.ps1 -Relogin'
        exit 1
    }

    # The generator names the variable TELEGRAM_SESSION_STRING by default,
    # or TELEGRAM_SESSION_STRING_<LABEL> if a label was typed at its prompt.
    $sessionLine = Select-String -Path $logFile -Pattern '^TELEGRAM_SESSION_STRING([A-Za-z0-9_]*)?=' |
        Select-Object -First 1 -ExpandProperty Line
    Remove-Item -Force $logFile -ErrorAction SilentlyContinue

    if ([string]::IsNullOrEmpty($sessionLine)) {
        Write-Failure 'Не удалось получить строку сессии из вывода входа.'
        exit 1
    }

    $splitIndex = $sessionLine.IndexOf('=')
    return @{
        Var   = $sessionLine.Substring(0, $splitIndex)
        Value = $sessionLine.Substring($splitIndex + 1)
    }
}

function Test-FullConfig {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $false }
    $content = Get-Content -Raw -Path $Path
    return ($content -match '(?m)^TELEGRAM_API_ID=.+$') -and
        ($content -match '(?m)^TELEGRAM_API_HASH=.+$') -and
        ($content -match '(?m)^TELEGRAM_SESSION_STRING[A-Za-z0-9_]*=.+$')
}

function Protect-ConfigFile {
    # NTFS equivalent of chmod 600: strip inherited permissions, grant the
    # current user only. Uses the fully-qualified identity (not bare
    # $env:USERNAME) so it resolves correctly on domain-joined machines too.
    param([string]$Path)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = Get-Acl $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) {
        $acl.RemoveAccessRule($rule) | Out-Null
    }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity, 'FullControl', 'Allow'
    )
    $acl.AddAccessRule($rule)
    Set-Acl -Path $Path -AclObject $acl
}

function Write-ConfigEnv {
    param([string]$ApiId, [string]$ApiHash, [string]$SessionVar, [string]$SessionValue)

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if (Test-Path $ConfigPath) {
        $backup = "$ConfigPath.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item -Path $ConfigPath -Destination $backup
    }

    $deviceModel = "telegram-mcp-ag ($env:COMPUTERNAME)"
    $lines = @(
        "TELEGRAM_API_ID=$ApiId"
        "TELEGRAM_API_HASH=$ApiHash"
        "$SessionVar=$SessionValue"
        'TELEGRAM_EXPOSED_TOOLS=read-only'
        "TELEGRAM_DEVICE_MODEL=$deviceModel"
    )
    $content = ($lines -join "`n") + "`n"

    # PowerShell 5.1's Out-File/Set-Content -Encoding utf8 always adds a
    # BOM, which python-dotenv would fold into the first key's name and
    # break config loading. WriteAllText with an explicit no-BOM encoding
    # behaves the same on 5.1 and 7.x.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($ConfigPath, $content, $utf8NoBom)

    Protect-ConfigFile -Path $ConfigPath
    Write-Ok "config.env записан ($ConfigPath, доступ только для вашей учётной записи)."
}

# ---------------------------------------------------------------------------
# Client registration
# ---------------------------------------------------------------------------

function Register-ClaudeCode {
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { return }
    Write-Info 'Обнаружен Claude Code -- регистрирую сервер...'
    claude mcp remove -s user $ServerName *> $null
    claude mcp add -s user $ServerName -- (Get-VenvServerExe) *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'Claude Code настроен (scope: user).'
        if ($script:AutoApproveTools) {
            $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
            Update-ClaudeSettingsPermission -SettingsPath $settingsPath -Action 'add'
        }
    } else {
        Write-Warn "Не удалось зарегистрировать сервер в Claude Code. Добавьте вручную: claude mcp add -s user $ServerName -- $(Get-VenvServerExe)"
    }
}

function Get-MaintenancePython {
    # Returns a usable python.exe path, or $null. Prefers the venv; falls
    # back to a throwaway venv built from system Python so `pip install
    # tomlkit` never touches a locked-down system interpreter.
    $venvPy = Get-VenvPython
    if (Test-Path $venvPy) { return $venvPy }

    $sysPy = Find-SystemPython
    if (-not $sysPy) { return $null }

    $tmpRoot = Join-Path $env:TEMP ([System.Guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null
    $tmpVenv = Join-Path $tmpRoot 'venv'
    Invoke-PythonCommand -PyCmd $sysPy -Arguments @('-m', 'venv', $tmpVenv) *> $null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
        return $null
    }
    $script:MaintenanceVenvs.Add($tmpRoot)
    return (Join-Path $tmpVenv 'Scripts\python.exe')
}

$script:CodexTomlUpsertScript = @'
import sys
import tomlkit

config_path, command_path, server_name = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(config_path, "r", encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())
except FileNotFoundError:
    doc = tomlkit.document()

servers = doc.setdefault("mcp_servers", tomlkit.table())
entry = tomlkit.table()
entry["command"] = command_path
entry["args"] = tomlkit.array()
servers[server_name] = entry

with open(config_path, "w", encoding="utf-8") as f:
    f.write(tomlkit.dumps(doc))
'@

$script:CodexTomlRemoveScript = @'
import sys
import tomlkit

config_path, server_name = sys.argv[1], sys.argv[2]

with open(config_path, "r", encoding="utf-8") as f:
    doc = tomlkit.parse(f.read())

servers = doc.get("mcp_servers")
if servers is not None and server_name in servers:
    del servers[server_name]
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
'@

$script:ClaudeSettingsPermissionScript = @'
import json
import sys

path, rule, action = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}

allow = data.setdefault("permissions", {}).setdefault("allow", [])
if action == "add":
    if rule not in allow:
        allow.append(rule)
else:
    data["permissions"]["allow"] = [r for r in allow if r != rule]

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
'@

$script:CodexTomlSetApprovalScript = @'
import sys
import tomlkit

config_path, server_name = sys.argv[1], sys.argv[2]

with open(config_path, "r", encoding="utf-8") as f:
    doc = tomlkit.parse(f.read())

servers = doc.get("mcp_servers")
if servers is not None and server_name in servers:
    servers[server_name]["default_tools_approval_mode"] = "approve"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
'@

$script:ClaudeDesktopJsonUpsertScript = @'
import json
import sys

path, command, server_name = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}

data.setdefault("mcpServers", {})[server_name] = {"command": command, "args": []}

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
'@

$script:ClaudeDesktopJsonRemoveScript = @'
import json
import sys

path, server_name = sys.argv[1], sys.argv[2]

with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

servers = data.get("mcpServers")
if servers is not None and server_name in servers:
    del servers[server_name]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
'@

function Invoke-PythonEdit {
    param([string]$PythonExe, [string]$ScriptBody, [string[]]$Arguments)
    $ScriptBody | & $PythonExe - @Arguments
    return ($LASTEXITCODE -eq 0)
}

# Adds or removes "mcp__$ServerName" in the top-level permissions.allow array
# of a Claude Code settings.json (user-scope by default -- matches the
# `-s user` registration). $Action is "add" or "remove"; safe either way.
function Update-ClaudeSettingsPermission {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$SettingsPath, [string]$Action)

    $py = Get-MaintenancePython
    if (-not $py) {
        Write-Warn "Не нашёл Python для правки $SettingsPath, пропускаю."
        return
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SettingsPath) | Out-Null
    if (Test-Path $SettingsPath) {
        Copy-Item -Path $SettingsPath -Destination "$SettingsPath.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    }

    $ok = Invoke-PythonEdit -PythonExe $py -ScriptBody $script:ClaudeSettingsPermissionScript `
        -Arguments @($SettingsPath, "mcp__$ServerName", $Action)
    if ($ok -and $Action -eq 'add') {
        Write-Ok "Claude Code: разрешение на инструменты чтения добавлено ($SettingsPath)."
    }
}

# `codex mcp add` has no flag for this, so it's always a follow-up TOML edit,
# whichever way the server entry itself got created. Requires the
# [mcp_servers.$ServerName] table to already exist.
function Set-CodexApproval {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$CodexConfig)

    $py = Get-MaintenancePython
    if (-not $py) {
        Write-Warn "Не нашёл Python для правки $CodexConfig, пропускаю разрешение автозапуска."
        return
    }
    & $py -m pip install -q tomlkit
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Не удалось установить tomlkit, пропускаю разрешение автозапуска."
        return
    }

    $ok = Invoke-PythonEdit -PythonExe $py -ScriptBody $script:CodexTomlSetApprovalScript -Arguments @($CodexConfig, $ServerName)
    if ($ok) { Write-Ok "Codex: разрешение на инструменты чтения добавлено ($CodexConfig)." }
}

function Update-CodexToml {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$CodexConfig)

    $py = Get-MaintenancePython
    if (-not $py) {
        Write-Warn "Не нашёл Python для правки $CodexConfig, пропускаю."
        return $false
    }
    & $py -m pip install -q tomlkit
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Не удалось установить tomlkit, пропускаю правку $CodexConfig."
        return $false
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $CodexConfig) | Out-Null
    if (Test-Path $CodexConfig) {
        Copy-Item -Path $CodexConfig -Destination "$CodexConfig.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    }

    $ok = Invoke-PythonEdit -PythonExe $py -ScriptBody $script:CodexTomlUpsertScript `
        -Arguments @($CodexConfig, (Get-VenvServerExe), $ServerName)
    if ($ok) { Write-Ok "config.toml обновлён ($CodexConfig)." }
    return $ok
}

function Register-Codex {
    $codexDir = Join-Path $env:USERPROFILE '.codex'
    $codexConfig = Join-Path $codexDir 'config.toml'

    $hasCodexCli = [bool](Get-Command codex -ErrorAction SilentlyContinue)
    if (-not $hasCodexCli -and -not (Test-Path $codexDir)) { return }

    Write-Info 'Обнаружен Codex CLI / ChatGPT Desktop -- регистрирую сервер...'

    if ($hasCodexCli) {
        codex mcp remove $ServerName *> $null
        codex mcp add $ServerName -- (Get-VenvServerExe) *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Codex настроен через 'codex mcp add'."
            if ($script:AutoApproveTools) { Set-CodexApproval -CodexConfig $codexConfig }
            return
        }
        Write-Warn "'codex mcp add' не сработал (команда экспериментальная), правлю $codexConfig напрямую."
    }

    if (Update-CodexToml -CodexConfig $codexConfig) {
        if ($script:AutoApproveTools) { Set-CodexApproval -CodexConfig $codexConfig }
    } else {
        Write-Warn 'Codex/ChatGPT Desktop не настроены автоматически. Смотрите examples/codex.config.toml.'
    }
}

function Register-ClaudeDesktop {
    $target = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
    $claudeDir = Join-Path $env:APPDATA 'Claude'
    if (-not (Test-Path $claudeDir)) { return }

    Write-Info 'Обнаружен Claude Desktop -- регистрирую сервер...'
    $py = Get-MaintenancePython
    if (-not $py) {
        Write-Warn "Не нашёл Python для правки $target, пропускаю."
        return
    }

    if (Test-Path $target) {
        Copy-Item -Path $target -Destination "$target.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    }

    $ok = Invoke-PythonEdit -PythonExe $py -ScriptBody $script:ClaudeDesktopJsonUpsertScript `
        -Arguments @($target, (Get-VenvServerExe), $ServerName)
    if ($ok) {
        Write-Ok "Claude Desktop настроен ($target). Перезапустите приложение, чтобы изменения применились."
        if ($script:AutoApproveTools) {
            Write-Info 'У Claude Desktop нет способа разрешить это заранее -- при первом вызове инструмента нажмите "Always Allow".'
        }
    } else {
        Write-Warn 'Claude Desktop не настроен автоматически. Добавьте сервер вручную по примеру examples/claude-code.mcp.json.'
    }
}

# Claude Code (and the local/SSH/"Code" sessions of the Claude Code Desktop
# app) reads personal skills from ~/.claude/skills/ -- copying ours there
# (from the same ref the package itself was installed from) is what makes
# "сделай сводку" work as a skill in *any* project on this machine, not just
# when working inside a checkout of this repo. Ordinary Claude Desktop chat
# does NOT read this directory: it loads skills synced from the user's
# claude.ai account (Settings -> Customize) -- see Install-ClaudeSkills below.
function Install-ClaudeSkill {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$Name)

    $dest = Join-Path $env:USERPROFILE ".claude\skills\$Name"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $url = "https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/$RepoRef/.claude/skills/$Name/SKILL.md"
    try {
        Invoke-WebRequest -Uri $url -OutFile (Join-Path $dest 'SKILL.md') -ErrorAction Stop
        Write-Ok "Скилл $Name установлен ($dest)."
    } catch {
        Write-Warn "Не удалось скачать скилл $Name -- не критично, остальное работает и без него."
        Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
    }
}

# claude.ai's "Upload a skill" dialog wants a zip with a top-level
# telegram-digest/ folder, not a bare SKILL.md -- this is the exact same
# file the README links to for readers who add the skill by hand, so both
# routes end up with byte-identical output and nobody has to zip anything
# themselves.
function Install-ClaudeSkillZip {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param()

    $out = Join-Path $InstallDir 'telegram-digest-skill.zip'
    $url = "https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/$RepoRef/.claude/skills/telegram-digest.zip"
    try {
        Invoke-WebRequest -Uri $url -OutFile $out -ErrorAction Stop
        # Also copy to Downloads -- that's where README tells users to look for
        # it, since it's a folder every non-developer already knows how to find
        # (unlike $InstallDir). -Force silently replaces a file left over from
        # a previous run, never an error.
        $downloadsDir = Join-Path $env:USERPROFILE 'Downloads'
        New-Item -ItemType Directory -Force -Path $downloadsDir | Out-Null
        $downloadsOut = Join-Path $downloadsDir 'telegram-digest-skill.zip'
        Copy-Item -Path $out -Destination $downloadsOut -Force -ErrorAction SilentlyContinue
        Write-Ok "Готовый архив скилла для Claude Desktop (в Загрузках): $downloadsOut"
    } catch {
        Write-Warn 'Не удалось скачать архив скилла для Claude Desktop -- не критично, остальное работает и без него.'
        Remove-Item -Force $out -ErrorAction SilentlyContinue
    }
}

function Install-ClaudeSkills {
    $haveCli = [bool](Get-Command claude -ErrorAction SilentlyContinue)
    $claudeDesktopDir = Join-Path $env:APPDATA 'Claude'
    $haveDesktop = Test-Path $claudeDesktopDir
    if (-not $haveCli -and -not $haveDesktop) { return }

    Write-Info 'Устанавливаю скилл Claude (сводка по Telegram)...'
    try {
        Install-ClaudeSkill -Name 'telegram-digest'
    } catch {
        Write-Warn 'Установка скилла не удалась -- не критично, остальное работает и без него.'
    }

    if ($haveDesktop) {
        try {
            Install-ClaudeSkillZip
        } catch {
            Write-Warn 'Не удалось подготовить архив скилла -- не критично, остальное работает и без него.'
        }
        Write-Info 'В обычном чате Claude Desktop скиллы читаются не с диска, а из вашего аккаунта claude.ai: чтобы сводка работала и там, подключите файл telegram-digest-skill.zip из Загрузок через значок профиля -> Settings -> Customize -> Skills -> Add -> Upload a skill (подробности в README).'
    }
}

# ---------------------------------------------------------------------------
# Self-check + summary
# ---------------------------------------------------------------------------

$script:SelfCheckScript = @'
import asyncio
import json
import sys

from telegram_mcp_ag.server import check_transcription_access

result = asyncio.run(check_transcription_access())
try:
    data = json.loads(result)
except json.JSONDecodeError:
    data = None

print(result)
sys.exit(0 if data and "is_premium" in data else 1)
'@

function Invoke-SelfCheck {
    Write-Host ''
    Write-Info 'Проверяю подключение к Telegram...'
    $py = Get-VenvPython
    $output = $script:SelfCheckScript | & $py - 2>&1
    $status = $LASTEXITCODE
    $outputText = ($output | Out-String).TrimEnd()

    if ($status -eq 0) {
        Write-Ok 'Подключение к Telegram работает.'
    } else {
        Write-Warn 'Самопроверка не прошла:'
        Write-Host $outputText
        Write-Warn "config.env записан, но сервер пока не отвечает как ожидалось. Запустите вручную: $(Get-VenvServerExe)"
    }
}

function Write-Summary {
    $errorLogScript = @'
import os
import telegram_mcp
package_dir = os.path.dirname(os.path.abspath(telegram_mcp.__file__))
print(os.path.join(os.path.dirname(package_dir), "mcp_errors.log"))
'@
    $py = Get-VenvPython
    $errorLog = ''
    try {
        $errorLog = ($errorLogScript | & $py - 2>$null | Out-String).Trim()
    } catch {
        $errorLog = ''
    }

    Write-Host ''
    Write-Ok "Готово! telegram-mcp-ag установлен в $InstallDir"
    Write-Host ''
    Write-Host 'Что дальше:'
    Write-Host '  - Перезапустите Claude Code / Codex / Claude Desktop, если они уже были открыты.'
    Write-Host "  - Проверить сервер вручную: $(Get-VenvServerExe)"
    if ($errorLog) {
        Write-Host "  - Лог ошибок сервера: $errorLog"
    }
    Write-Host '  - Повторный вход в Telegram (например, после отзыва сессии): .\install.ps1 -Relogin'
    Write-Host '  - Полное удаление: .\install.ps1 -Uninstall'
    Write-Host ''
    Write-Host "Секреты лежат в $ConfigPath (доступ только для вашей учётной записи). Никому их не показывайте и не публикуйте."
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

function Remove-CodexTomlEntry {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$CodexConfig)
    if (-not (Test-Path $CodexConfig)) { return }

    $py = Get-MaintenancePython
    if (-not $py) {
        Write-Warn "Не нашёл Python -- удалите вручную блок [mcp_servers.$ServerName] из $CodexConfig."
        return
    }
    & $py -m pip install -q tomlkit 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Не удалось установить tomlkit -- удалите вручную блок [mcp_servers.$ServerName] из $CodexConfig."
        return
    }

    Copy-Item -Path $CodexConfig -Destination "$CodexConfig.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    $ok = Invoke-PythonEdit -PythonExe $py -ScriptBody $script:CodexTomlRemoveScript -Arguments @($CodexConfig, $ServerName)
    if ($ok) { Write-Ok "Запись Codex удалена из $CodexConfig." }
}

function Remove-ClaudeDesktopJsonEntry {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$Target)
    if (-not (Test-Path $Target)) { return }

    $py = Get-MaintenancePython
    if (-not $py) {
        Write-Warn "Не нашёл Python -- удалите вручную запись `"$ServerName`" из $Target."
        return
    }

    Copy-Item -Path $Target -Destination "$Target.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    $ok = Invoke-PythonEdit -PythonExe $py -ScriptBody $script:ClaudeDesktopJsonRemoveScript -Arguments @($Target, $ServerName)
    if ($ok) { Write-Ok "Запись Claude Desktop удалена из $Target." }
}

function Invoke-Uninstall {
    Write-Info 'Снимаю регистрации из клиентов...'

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        claude mcp remove -s user $ServerName *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok 'Claude Code: сервер удалён.' }
        $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
        Update-ClaudeSettingsPermission -SettingsPath $settingsPath -Action 'remove'
    }

    foreach ($name in @('telegram-digest', 'setup-telegram-mcp')) {
        $skillDir = Join-Path $env:USERPROFILE ".claude\skills\$name"
        if (Test-Path $skillDir) { Remove-Item -Recurse -Force $skillDir }
    }

    $codexConfig = Join-Path $env:USERPROFILE '.codex\config.toml'
    if (Get-Command codex -ErrorAction SilentlyContinue) {
        codex mcp remove $ServerName *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok 'Codex: сервер удалён.'
        } else {
            Remove-CodexTomlEntry -CodexConfig $codexConfig
        }
    } else {
        Remove-CodexTomlEntry -CodexConfig $codexConfig
    }

    $claudeDesktopConfig = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
    Remove-ClaudeDesktopJsonEntry -Target $claudeDesktopConfig

    if (Test-Path $InstallDir) {
        if (Confirm-Action "Удалить папку $InstallDir вместе с сохранённой сессией?") {
            Remove-Item -Recurse -Force $InstallDir
            Write-Ok "Папка $InstallDir удалена."
        } else {
            Write-Info "Папка $InstallDir оставлена без изменений."
        }
    }

    Write-Host ''
    Write-Warn "Не забудьте отозвать сессию в самом Telegram: Settings -> Devices -> найдите устройство `"telegram-mcp-ag ($env:COMPUTERNAME)`" и завершите его."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

function Invoke-Main {
    param([switch]$Uninstall, [switch]$Relogin, [switch]$Qr, [switch]$Phone)

    Test-SupportedPlatform

    if ($Uninstall) {
        Invoke-Uninstall
        return
    }

    Initialize-PythonAndVenv
    Install-ServerPackage
    Confirm-NewVenv

    if (-not $Relogin -and (Test-FullConfig -Path $ConfigPath)) {
        Write-Info "Найден существующий $ConfigPath -- использую его без повторного входа."
        Write-Info 'Чтобы войти заново (например, после отзыва сессии), запустите: .\install.ps1 -Relogin'
    } else {
        $creds = Read-ApiCredentials
        $session = Invoke-TelegramLogin -ApiId $creds.ApiId -ApiHash $creds.ApiHash -Qr:$Qr -Phone:$Phone
        Write-ConfigEnv -ApiId $creds.ApiId -ApiHash $creds.ApiHash -SessionVar $session.Var -SessionValue $session.Value
    }

    $script:AutoApproveTools = Request-AutoApprove

    # Each step degrades independently: a client that isn't installed, or
    # an unexpected error registering one of them, must not stop the rest
    # from being tried -- mirrors install.sh's per-client resilience.
    foreach ($step in @('Register-ClaudeCode', 'Register-Codex', 'Register-ClaudeDesktop', 'Install-ClaudeSkills', 'Invoke-SelfCheck')) {
        try {
            & $step
        } catch {
            Write-Warn "Шаг '$step' пропущен из-за ошибки: $($_.Exception.Message)"
        }
    }
    Write-Summary
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Main -Uninstall:$Uninstall -Relogin:$Relogin -Qr:$Qr -Phone:$Phone
    } finally {
        Restore-VenvBackupOnFailure
        Remove-MaintenanceVenvs
    }
}
