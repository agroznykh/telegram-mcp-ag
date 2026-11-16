#Requires -Version 5.1
<#
.SYNOPSIS
    Installer for telegram-mcp-ag (Windows).

.DESCRIPTION
    Creates a venv through uv (which finds or downloads Python), installs the package,
    logs into Telegram, writes config.env, and registers the server with
    whichever of Claude Code / Codex CLI / ChatGPT Desktop / Claude Desktop
    are present. Ports install.sh (macOS/Linux) -- see that file for the
    shared design notes.

    All user-facing text is Russian: this installer targets people who have
    never opened a terminal before (see CLAUDE.md). Code comments are English.

.PARAMETER Relogin
    Redo the Telegram login (e.g. after the session was revoked manually in
    Settings -> Devices), even if config.env already exists. With several
    accounts, asks which one (or use -Account).

.PARAMETER Account
    With -Relogin: the label of the account to sign in again.

.PARAMETER AddAccount
    Sign in a second (third...) Telegram account. Asks for its label.

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
    .\install.ps1 -Relogin -Account work
    .\install.ps1 -AddAccount
#>
[CmdletBinding()]
param(
    [switch]$Relogin,
    [switch]$AddAccount,
    [string]$Account,
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

# Windows PowerShell 5.1 on older .NET still defaults to TLS 1.0, which GitHub
# rejects. Adds TLS 1.2 to whatever is already enabled.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$RepoArchiveBase = 'https://github.com/agroznykh/telegram-mcp-ag/archive'
$RepoRawBase = 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag'
$UpstreamArchiveBase = 'https://github.com/chigwell/telegram-mcp/archive'
# The server gets its own uv-managed Python, not one that belongs to another
# program, which could remove it and take the venv down too. Change this only
# after the new version passes CI and a live QR login.
$PythonVersion = '3.14'
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
# See Get-UpstreamSource. $script:UpstreamSource stays empty on the pip fallback path.
$UpstreamBaseDir = Join-Path $InstallDir 'upstream'
$script:UpstreamSource = $null
# Filled by Get-RefPyproject and Install-ServerPackage; see there.
$script:RefPyproject = $null
$script:InstalledVersion = ''
$ConfigPath = Join-Path $InstallDir 'config.env'
$ServerName = 'telegram-mcp-ag'

# The command a person can paste to run this installer again with a flag. The
# installer normally runs from a temp file, so there is no install.ps1 in the
# current folder to point them at.
function Get-InstallerCommand {
    param([string]$Flag)
    '$f = "$env:TEMP\telegram-mcp-ag-install.ps1"; iwr ''https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1'' -OutFile $f; powershell -NoProfile -ExecutionPolicy Bypass -File $f ' + $Flag
}

# Filled by Read-AccountList: canonical labels and the label of the default
# account ('' if none). They come only from telegram-mcp-ag-login --list-accounts:
# config.env is never parsed here for accounts.
$script:AccountLabels = @()
$script:AccountDefault = ''
# The account the final self-check asks about; '' means "no account argument".
$script:SelfCheckAccount = ''

$script:MaintenanceVenvs = New-Object System.Collections.Generic.List[string]
# Read by Register-ClaudeCode()/Register-Codex(). Always on, without asking:
# the server is read-only, so a confirmation on every call protects nothing.
$script:AutoApproveTools = $true

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
# Python / venv setup. uv provides its own $PythonVersion, reusing a copy it
# already has; whatever Python the machine has is not used.
# ---------------------------------------------------------------------------

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
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Install-Uv
    }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Failure 'Не удалось установить uv. Проверьте интернет и запустите установщик ещё раз.'
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

    Write-Info "Создаю виртуальное окружение на Python $PythonVersion (если его ещё нет, он скачается)..."
    # --seed: the client-config helpers run "$py -m pip install tomlkit".
    uv venv --seed --managed-python --python $PythonVersion $VenvDir | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Failure "Не удалось скачать Python $PythonVersion. Проверьте интернет и запустите установщик ещё раз."
        exit 1
    }
    Write-Ok "Окружение готово: $VenvDir"
}

function Get-VenvPython { Join-Path $VenvDir 'Scripts\python.exe' }
function Get-VenvServerExe { Join-Path $VenvDir 'Scripts\telegram-mcp-ag.exe' }
function Get-VenvLoginExe { Join-Path $VenvDir 'Scripts\telegram-mcp-ag-login.exe' }

# Downloads the pinned upstream source, so installing needs no git. An archive
# URL won't do: upstream's install_guard accepts only a git or file:// install,
# and on every start checks that the source dir is still there -- which is why
# it lives in $InstallDir rather than a temp dir. The SHA comes from the
# release's own pyproject.toml, the one place it is pinned.
function Get-UpstreamSource {
    $sha = $null
    $match = [regex]::Match((Get-RefPyproject), 'chigwell/telegram-mcp\.git@([0-9a-f]{40})')
    if ($match.Success) { $sha = $match.Groups[1].Value }
    if (-not $sha) {
        Write-Failure 'Не удалось узнать, какую версию компонентов скачивать. Проверьте интернет и запустите установщик ещё раз.'
        exit 1
    }
    $script:UpstreamSource = Join-Path $UpstreamBaseDir $sha
    if (Test-Path (Join-Path $script:UpstreamSource 'pyproject.toml')) { return }

    Write-Info 'Скачиваю компоненты сервера...'
    # A short name: the zip's own top-level directory already repeats the SHA,
    # and Expand-Archive in PowerShell 5.1 fails past 260 characters in a path.
    $partial = Join-Path $UpstreamBaseDir 'p'
    $archive = "$partial.zip"
    New-Item -ItemType Directory -Force -Path $UpstreamBaseDir | Out-Null
    if (Test-Path $partial) { Remove-Item -Recurse -Force $partial }
    try {
        $previousProgress = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri "$UpstreamArchiveBase/$sha.zip" -OutFile $archive -UseBasicParsing -ErrorAction Stop
        Expand-Archive -Path $archive -DestinationPath $partial -ErrorAction Stop
        $ProgressPreference = $previousProgress
    } catch {
        Write-Failure 'Не удалось скачать компоненты сервера. Проверьте интернет и запустите установщик ещё раз.'
        exit 1
    } finally {
        Remove-Item -Force $archive -ErrorAction SilentlyContinue
    }
    # GitHub zips hold a single top-level telegram-mcp-<sha> directory.
    $extracted = Get-ChildItem -Path $partial -Directory | Select-Object -First 1
    if (Test-Path $script:UpstreamSource) { Remove-Item -Recurse -Force $script:UpstreamSource }
    Move-Item -Path $extracted.FullName -Destination $script:UpstreamSource
    Remove-Item -Recurse -Force $partial
}

# pyproject.toml of $RepoRef, downloaded once: it gives both the version shown
# to the user and the upstream SHA. '' if the download fails or stalls.
function Get-RefPyproject {
    if ($null -eq $script:RefPyproject) {
        try {
            $script:RefPyproject = [string](Invoke-RestMethod -Uri "$RepoRawBase/$RepoRef/pyproject.toml" -TimeoutSec 30 -ErrorAction Stop)
        } catch {
            Write-Verbose "Could not read pyproject.toml: $($_.Exception.Message)"
            $script:RefPyproject = ''
        }
    }
    return $script:RefPyproject
}

# Version of the package about to be installed; empty if it cannot be read
# (the install itself does not depend on it).
function Get-RefVersion {
    $match = [regex]::Match((Get-RefPyproject), '(?m)^version\s*=\s*"([^"]+)"')
    if ($match.Success) { return $match.Groups[1].Value }
    return ''
}

# Version of the package installed in the venv; empty if it cannot be read.
function Get-InstalledVersion {
    try {
        $py = Get-VenvPython
        $version = (& $py -c 'import telegram_mcp_ag; print(telegram_mcp_ag.__version__)' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0) { return $version }
    } catch {
        Write-Verbose "Could not read the installed version: $($_.Exception.Message)"
    }
    return ''
}

# " 0.4.1" for a known version, "" otherwise -- to splice into a message.
function Format-Version {
    param([string]$Version)
    if ($Version) { return " $Version" }
    return ''
}

function Install-ServerPackage {
    Write-Info "Устанавливаю telegram-mcp-ag$(Format-Version (Get-RefVersion)) (ref: $RepoRef)..."
    $py = Get-VenvPython
    $packageUrl = "$RepoArchiveBase/$RepoRef.tar.gz"
    # Reinstalling: the upstream telegram-mcp is pinned by SHA but keeps the
    # same version string across SHAs, so a plain install on an existing venv
    # sees "already satisfied" and leaves the old upstream under new code.
    Get-UpstreamSource
    $sourceUri = & $py -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).as_uri())' $script:UpstreamSource
    $overrides = [System.IO.Path]::GetTempFileName()
    Set-Content -Path $overrides -Encoding Ascii -Value "telegram-mcp @ $sourceUri"
    uv pip install -q --python $py --reinstall --overrides $overrides $packageUrl
    $installExit = $LASTEXITCODE
    Remove-Item -Force $overrides -ErrorAction SilentlyContinue
    if ($installExit -ne 0) {
        Write-Failure 'Не удалось установить пакет.'
        exit 1
    }
    $script:InstalledVersion = Get-InstalledVersion
    Write-Ok "Пакет telegram-mcp-ag$(Format-Version $script:InstalledVersion) установлен."
}

# The venv rebuild is confirmed good at this point (Initialize-PythonAndVenv
# and Install-ServerPackage both already succeeded) -- the backup is no
# longer needed, and dropping it here is what makes
# Restore-VenvBackupOnFailure a no-op on a successful run.
function Confirm-NewVenv {
    if (Test-Path $VenvBackupDir) {
        Remove-Item -Recurse -Force $VenvBackupDir
    }
    # Older upstream copies had to survive until now: the backup venv needed them.
    if ($script:UpstreamSource) {
        Get-ChildItem -Path $UpstreamBaseDir -Directory |
            Where-Object { $_.FullName -ne $script:UpstreamSource } |
            Remove-Item -Recurse -Force
    }
}

# What keeps the install folder from being replaced. Windows refuses to rename
# or delete files another process has open, and the server (started by Claude
# Desktop, Claude Code, Codex...) holds its own files open -- Claude Desktop
# even starts it again the moment a new one appears. Returns display names:
# the server itself when it runs out of $VenvDir, and Claude Desktop when a
# venv already exists (it would restart the server mid-install).
function Get-InstallHolder {
    $holders = @()
    if (-not (Test-Path $VenvDir)) { return $holders }
    $venvPrefix = $VenvDir.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    foreach ($process in (Get-Process -ErrorAction SilentlyContinue)) {
        $path = $null
        try { $path = $process.Path } catch { $path = $null }
        if (-not $path) { continue }
        if ($path.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $holders += 'сервер telegram-mcp-ag'
        } elseif ($path -like '*\WindowsApps\Claude_*\app\Claude.exe') {
            $holders += 'Claude Desktop'
        }
    }
    return @($holders | Select-Object -Unique)
}

# Asks the person to close what holds the install and waits. Never closes
# anything itself: they may have unsaved work in it.
function Wait-InstallFree {
    while ($true) {
        $holders = @(Get-InstallHolder)
        if ($holders.Count -eq 0) { return }
        Write-Warn ('Сейчас запущено: ' + ($holders -join ', ') + '. Пока оно работает, Windows не даёт заменить файлы сервера.')
        Write-Info 'Закройте Claude Desktop полностью: крестик на окне его не закрывает. Нажмите правой кнопкой на значок Claude в области уведомлений (справа внизу, рядом с часами; может быть под стрелкой ^) и выберите "Выход". Если не получилось -- перезагрузите компьютер и запустите установщик снова. Другие программы, где был открыт Telegram (Claude Code, Codex), тоже закройте.'
        $answer = Read-Answer -Prompt 'Когда закроете, нажмите Enter. Чтобы отменить установку, введите q'
        if ($answer -match '^[QqЙй]$') {
            Write-Info 'Установка отменена. Ничего не изменено.'
            exit 1
        }
    }
}

# Called from the top-level `finally` (runs on every exit, success or
# failure) -- a backup still present means the rebuild was interrupted
# before Confirm-NewVenv ran, so put the old, working venv back rather than
# leave the user with neither a working old one nor a finished new one.
function Restore-VenvBackupOnFailure {
    if (Test-Path $VenvBackupDir) {
        try {
            if (Test-Path $VenvDir) {
                Remove-Item -Recurse -Force $VenvDir
            }
            Move-Item -Path $VenvBackupDir -Destination $VenvDir
        } catch {
            Write-Failure "Не удалось вернуть прежнюю установку: файлы заняты запущенной программой (скорее всего, Claude Desktop). Закройте её полностью и запустите установщик ещё раз. Прежняя копия лежит в $VenvBackupDir."
        }
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

$script:LoginExitCode = 0

# One run of the login command; its exit code is left in $script:LoginExitCode.
# Not returned: the function's output must stay unredirected, or the QR code and
# the questions the command asks would be swallowed by whoever captures it. A
# function so that tests can stand in for the command.
function Invoke-LoginProcess {
    param([string]$ApiId, [string]$ApiHash, [string[]]$Arguments)
    $env:TELEGRAM_API_ID = $ApiId
    $env:TELEGRAM_API_HASH = $ApiHash
    $env:PYTHONUNBUFFERED = '1'
    try {
        & (Get-VenvLoginExe) @Arguments
        $script:LoginExitCode = $LASTEXITCODE
    } finally {
        Remove-Item Env:\TELEGRAM_API_ID, Env:\TELEGRAM_API_HASH, Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue
    }
}

function Invoke-TelegramLogin {
    param([string]$ApiId, [string]$ApiHash, [string]$Account, [switch]$Qr, [switch]$Phone)

    Write-Host ''
    if ($Account) {
        Write-Info "Вход в аккаунт `"$Account`". В приложении Telegram должен быть сейчас открыт именно он."
    }
    Write-Info 'Вход в Telegram.'
    Write-Info 'В приложении Telegram: Настройки -> Устройства -> Подключить устройство -> отсканировать QR-код.'
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

    # The login command merges the session into config.env (keeping that
    # file's ACL), and falls back to a phone-number login on its own if the
    # QR code is never scanned. It prints the session nowhere. Exit codes:
    # 3 -- Telegram rejected api_id/api_hash; 4 -- several accounts and none
    # named; 5 -- the person said it is the wrong account (nothing was saved).
    while ($true) {
        $loginArgs = @()
        if (-not $useQr) { $loginArgs += '--phone' }
        if ($Account) { $loginArgs += @('--account', $Account) }

        Invoke-LoginProcess -ApiId $ApiId -ApiHash $ApiHash -Arguments $loginArgs
        $status = $script:LoginExitCode

        if ($status -eq 0) { return }
        if ($status -eq 3) {
            $creds = Read-ApiCredentials
            $ApiId = $creds.ApiId
            $ApiHash = $creds.ApiHash
            Save-ApiCredentials -ApiId $ApiId -ApiHash $ApiHash | Out-Null
            continue
        }
        if ($status -eq 4) {
            Read-AccountList | Out-Null
            $Account = Read-AccountChoice -Question 'В какой аккаунт входить?'
            continue
        }
        if ($status -eq 5) {
            Write-Info 'Хорошо, ничего не сохранено.'
            if (Confirm-Action 'Попробовать войти ещё раз?') { continue }
        }
        $retry = '-Relogin'
        if ($Account) { $retry = "-Relogin -Account $Account" }
        Write-Failure "Не удалось войти в Telegram. Запустите вход ещё раз: $(Get-InstallerCommand $retry)"
        exit 1
    }
}

# api_id/api_hash don't change on a relogin -- they belong to the app on
# my.telegram.org, not to the session -- so a config.env that already has
# them (a relogin, or a session the server cleared after Telegram revoked
# it) is reused without asking again. $null if either is missing.
function Read-ConfigCredentials {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '', Justification = 'Returns both api_id and api_hash; plural is accurate.')]
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    $content = Get-Content -Raw -Path $Path
    if ($content -notmatch '(?m)^TELEGRAM_API_ID="?([^"\r\n]+)"?\r?$') { return $null }
    $apiId = $Matches[1]
    if ($content -notmatch '(?m)^TELEGRAM_API_HASH="?([^"\r\n]+)"?\r?$') { return $null }
    $apiHash = $Matches[1]
    return @{ ApiId = $apiId; ApiHash = $apiHash }
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
    param([string]$ApiId, [string]$ApiHash)

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if (Test-Path $ConfigPath) {
        $backup = "$ConfigPath.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item -Path $ConfigPath -Destination $backup
    }

    $deviceModel = "telegram-mcp-ag ($env:COMPUTERNAME)"
    $lines = @(
        "TELEGRAM_API_ID=$ApiId"
        "TELEGRAM_API_HASH=$ApiHash"
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
    Write-Ok 'api_id и api_hash сохранены.'
}

function Test-SessionKeys {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $false }
    $content = Get-Content -Raw -Path $Path
    return [bool]($content -match '(?m)^TELEGRAM_SESSION_(STRING|NAME)[A-Za-z0-9_]*=.+$')
}

$script:SaveApiCredentialsScript = @'
import os
from telegram_mcp_ag import config

config.write_values(
    {"TELEGRAM_API_ID": os.environ["TELEGRAM_API_ID"], "TELEGRAM_API_HASH": os.environ["TELEGRAM_API_HASH"]},
    config.CONFIG_PATH,
)
'@

# After Telegram rejects api_id/api_hash: when config.env already holds sessions
# only the two values are replaced, in place (write_values keeps every other line
# and the file's ACL). Write-ConfigEnv writes the file from scratch and would
# erase the accounts' sessions. The values travel in the environment, not in
# arguments.
function Save-ApiCredentials {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param([string]$ApiId, [string]$ApiHash)
    if (-not (Test-SessionKeys -Path $ConfigPath)) {
        Write-ConfigEnv -ApiId $ApiId -ApiHash $ApiHash
        return
    }
    $env:TELEGRAM_API_ID = $ApiId
    $env:TELEGRAM_API_HASH = $ApiHash
    try {
        $saved = Invoke-PythonEdit -PythonExe (Get-VenvPython) -ScriptBody $script:SaveApiCredentialsScript -Arguments @()
    } finally {
        Remove-Item Env:\TELEGRAM_API_ID, Env:\TELEGRAM_API_HASH -ErrorAction SilentlyContinue
    }
    if (-not $saved) {
        Write-Failure 'Не удалось обновить api_id и api_hash в config.env. Сохранённые сессии не тронуты.'
        exit 1
    }
    Write-Ok 'api_id и api_hash обновлены (сохранённые сессии не тронуты).'
}

# api_id/api_hash for a login: the saved ones, or ask and save. Returns
# @{ ApiId; ApiHash }.
function Initialize-ApiCredentials {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '', Justification = 'Returns both api_id and api_hash; plural is accurate.')]
    param()
    $creds = Read-ConfigCredentials -Path $ConfigPath
    if ($creds) {
        Write-Info 'Использую сохранённые api_id и api_hash.'
        return $creds
    }
    $creds = Read-ApiCredentials
    Save-ApiCredentials -ApiId $creds.ApiId -ApiHash $creds.ApiHash | Out-Null
    return $creds
}

# ---------------------------------------------------------------------------
# Accounts. The installer never reads config.env for them: the labels come from
# `telegram-mcp-ag-login --list-accounts`, the changes go through its options.
# ---------------------------------------------------------------------------

# Runs the login command and returns @{ Status; Output } with its standard output
# as one string; standard error is dropped. Python is told to write UTF-8: to a
# pipe it would use the console's ANSI code page, which cannot encode Russian
# (the "(по умолчанию)" mark). A function so that tests can stand in for it.
function Get-LoginCliOutput {
    param([string[]]$Arguments)
    $exe = Get-VenvLoginExe
    if (-not (Test-Path $exe)) { return @{ Status = 127; Output = '' } }
    # Native stderr would become a terminating error under 'Stop' in 5.1.
    $ErrorActionPreference = 'Continue'
    $previous = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $text = (& $exe @Arguments 2>$null | Out-String)
        $status = $LASTEXITCODE
    } finally {
        if ($null -eq $previous) {
            Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $previous
        }
    }
    return @{ Status = $status; Output = $text }
}

# Runs the login command for its effect, its output shown to the person;
# returns the exit code. A function so that tests can stand in for it.
function Invoke-LoginCli {
    param([string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    & (Get-VenvLoginExe) @Arguments | Out-Host
    return $LASTEXITCODE
}

# Whether the installed telegram-mcp-ag-login knows -Account. The installer is
# downloaded from `main` but the package comes from the latest release, so right
# after the multi-account code lands in main the package may still be older.
function Test-LoginSupportsAccounts {
    $result = Get-LoginCliOutput -Arguments @('--help')
    return [bool]($result.Output -like '*--account*')
}

function Assert-AccountsSupported {
    param([string]$What)
    if (Test-LoginSupportsAccounts) { return }
    Write-Failure "$What появится в следующей версии: установлен пакет, который ещё не умеет работать с несколькими аккаунтами. Ничего не изменено."
    exit 1
}

# Fills $script:AccountLabels and $script:AccountDefault; $false if the list
# could not be read. A line is "метка" or "метка (по умолчанию)": only the first
# word is used, and only if it is a canonical label, since it ends up in
# commands the person will paste.
function Read-AccountList {
    $script:AccountLabels = @()
    $script:AccountDefault = ''
    $result = Get-LoginCliOutput -Arguments @('--list-accounts')
    if ($result.Status -ne 0) { return $false }
    $labels = @()
    foreach ($line in ($result.Output -split '\r?\n')) {
        $text = $line.Trim()
        if (-not $text) { continue }
        $label = ($text -split '\s+')[0]
        if ($label -cnotmatch '^[a-z0-9_]{1,32}$') { continue }
        $labels += $label
        if ($text -match '^\S+\s+\(') { $script:AccountDefault = $label }
    }
    $script:AccountLabels = $labels
    return $true
}

# "метка1 (по умолчанию), метка2" for messages.
function Format-Accounts {
    $parts = @()
    foreach ($label in $script:AccountLabels) {
        if ($label -eq $script:AccountDefault) {
            $parts += "$label (по умолчанию)"
        } else {
            $parts += $label
        }
    }
    return ($parts -join ', ')
}

$script:ValidateLabelScript = @'
import sys
from telegram_mcp_ag import config

try:
    print(config.validate_label(sys.argv[1], allow_default=sys.argv[2] == "1"))
except config.ConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(2)
'@

# The canonical form of a label, or $null (the reason is printed, in Russian) if
# it is not allowed (exit code 2 of the script). Any other failure (a broken venv,
# a traceback) stops the installer, so that a caller does not ask again forever.
# The check is the package's own, so the installer and the server cannot disagree.
function Get-ValidatedLabel {
    param([string]$Label, [bool]$AllowDefault)
    $allow = '0'
    if ($AllowDefault) { $allow = '1' }
    $output = Invoke-PythonScript -PythonExe (Get-VenvPython) -ScriptBody $script:ValidateLabelScript -Arguments @($Label, $allow)
    $status = $LASTEXITCODE
    if ($status -eq 2) { return $null }
    if ($status -ne 0) {
        Write-Failure "Не удалось проверить название: установленный пакет не запускается. Запустите установщик ещё раз: $(Get-InstallerCommand '')"
        exit 1
    }
    return ([string]($output | Out-String)).Trim()
}

# Asks for a label until it is allowed and free. With -AllowEmpty an empty answer
# means "keep as it is" and returns '' -- or -EmptyLabel, if given.
function Read-NewLabel {
    param([string]$Prompt, [string[]]$Taken = @(), [switch]$AllowEmpty, [string]$EmptyLabel = '')
    while ($true) {
        $reply = Read-Answer $Prompt
        if ([string]::IsNullOrWhiteSpace($reply)) {
            if ($AllowEmpty) { return $EmptyLabel }
            Write-Warn 'Название нужно: одно слово латиницей, например work.'
            continue
        }
        $label = Get-ValidatedLabel -Label $reply -AllowDefault $false
        if (-not $label) { continue }
        # 'none' is how --set-default says "no default account": an account with
        # that name could never be chosen as the default.
        if ($label -eq 'none') {
            Write-Warn 'Название "none" занято под служебное значение, выберите другое.'
            continue
        }
        if ($Taken -contains $label) {
            Write-Warn "Название `"$label`" уже занято, выберите другое."
            continue
        }
        return $label
    }
}

# Shows the accounts as a numbered list and returns the label of the chosen one.
function Read-AccountChoice {
    param([string]$Question)
    $labels = @($script:AccountLabels)
    if ($labels.Count -eq 0) {
        Write-Failure "Не нашёл сохранённых аккаунтов. Сначала выполните установку без флагов: $(Get-InstallerCommand '')"
        exit 1
    }
    while ($true) {
        Write-Host ''
        Write-Info $Question
        for ($i = 0; $i -lt $labels.Count; $i++) {
            Write-Host ('  {0}) {1}' -f ($i + 1), $labels[$i])
        }
        $reply = Read-Answer 'Введите номер'
        $number = 0
        if ([int]::TryParse($reply, [ref]$number) -and $number -ge 1 -and $number -le $labels.Count) {
            return $labels[$number - 1]
        }
        Write-Warn 'Нужно ввести номер из списка.'
    }
}

# Which account answers when the chat does not name one. Returns the label, or ''
# for "always ask". Enter keeps -Current.
function Read-DefaultAccountChoice {
    param([string[]]$Labels, [string]$Current)
    while ($true) {
        Write-Host ''
        Write-Info 'Какой аккаунт использовать, когда вы не говорите, какой именно?'
        for ($i = 0; $i -lt $Labels.Count; $i++) {
            $mark = ''
            if ($Labels[$i] -eq $Current) { $mark = ' (сейчас)' }
            Write-Host ('  {0}) {1}{2}' -f ($i + 1), $Labels[$i], $mark)
        }
        Write-Host '  0) никакой -- каждый раз спрашивать, какой нужен'
        $hint = 'Enter -- никакой'
        if ($Current) { $hint = "Enter -- $Current, как сейчас" }
        $reply = Read-Answer "Введите номер ($hint)"
        if ([string]::IsNullOrWhiteSpace($reply)) { return $Current }
        if ($reply.Trim() -eq '0') { return '' }
        $number = 0
        if ([int]::TryParse($reply, [ref]$number) -and $number -ge 1 -and $number -le $Labels.Count) {
            return $Labels[$number - 1]
        }
        Write-Warn 'Нужно ввести номер из списка.'
    }
}

function Invoke-AddAccount {
    param([switch]$Qr, [switch]$Phone)

    Assert-AccountsSupported 'Добавление аккаунта'
    if (-not (Test-FullConfig -Path $ConfigPath) -or -not (Read-AccountList) -or $script:AccountLabels.Count -eq 0) {
        Write-Failure "Первого аккаунта ещё нет. Сначала выполните обычную установку: $(Get-InstallerCommand '')"
        exit 1
    }

    Write-Host ''
    Write-Info "Сейчас подключено: $(Format-Accounts)."
    Write-Info 'Новому аккаунту нужно название -- по нему вы будете выбирать его в чате. Одно слово латиницей: например, work для рабочего или personal для личного.'
    $currentDefault = $script:AccountDefault
    $existing = @($script:AccountLabels)
    $newLabel = Read-NewLabel -Prompt 'Название нового аккаунта' -Taken ($existing + @('default'))

    $firstName = ''
    if ($existing -contains 'default') {
        Write-Host ''
        Write-Info 'Первый аккаунт пока без названия. Лучше его назвать, чтобы в чате различать оба.'
        if ($newLabel -eq 'personal') {
            # Enter cannot stand for a name the new account has just taken.
            $firstName = Read-NewLabel -Prompt 'Название первого аккаунта (Enter -- оставить без названия)' -Taken ($existing + @($newLabel)) -AllowEmpty
        } else {
            $firstName = Read-NewLabel -Prompt 'Название первого аккаунта (Enter -- personal)' -Taken ($existing + @($newLabel)) -AllowEmpty -EmptyLabel 'personal'
        }
    }

    $after = @()
    foreach ($label in $existing) {
        if ($label -eq 'default' -and $firstName) {
            $after += $firstName
        } else {
            $after += $label
        }
    }
    $after += $newLabel
    $renamedDefault = $currentDefault
    if ($currentDefault -eq 'default' -and $firstName) { $renamedDefault = $firstName }
    $chosenDefault = Read-DefaultAccountChoice -Labels $after -Current $renamedDefault

    $creds = Initialize-ApiCredentials
    # The login goes first: if it fails or is declined, nothing has changed yet.
    Invoke-TelegramLogin -ApiId $creds.ApiId -ApiHash $creds.ApiHash -Account $newLabel -Qr:$Qr -Phone:$Phone

    # What the default is now, so that it is rewritten only if the choice differs.
    $nowDefault = $currentDefault
    if ($firstName) {
        if ((Invoke-LoginCli -Arguments @('--rename-default', $firstName)) -eq 0) {
            $nowDefault = $renamedDefault
        } else {
            Write-Warn "Не удалось дать первому аккаунту название `"$firstName`": он остался без названия. Аккаунт `"$newLabel`" добавлен."
            if ($chosenDefault -eq $firstName) { $chosenDefault = 'default' }
        }
    }
    if ($chosenDefault -ne $nowDefault) {
        $target = $chosenDefault
        if (-not $target) { $target = 'none' }
        if ((Invoke-LoginCli -Arguments @('--set-default', $target)) -ne 0) {
            Write-Warn "Не удалось запомнить аккаунт по умолчанию. Повторите: $(Get-VenvLoginExe) --set-default $target"
        }
    }

    $script:SelfCheckAccount = $newLabel
    Write-Host ''
    Write-Ok "Аккаунт `"$newLabel`" добавлен."
    Write-Warn 'Сервер читает настройки при запуске: перезапустите Claude, Codex или ChatGPT, чтобы он увидел новый аккаунт.'
}

function Invoke-ReloginAccount {
    param([string]$Account, [switch]$Qr, [switch]$Phone)

    $label = ''
    if ($Account) {
        Assert-AccountsSupported 'Перелогин по названию аккаунта (-Account)'
        $label = Get-ValidatedLabel -Label $Account -AllowDefault $true
        if (-not $label) { exit 1 }
        if (-not (Read-AccountList) -or ($script:AccountLabels -notcontains $label)) {
            $known = ''
            if ($script:AccountLabels.Count -gt 0) { $known = ". Есть: $($script:AccountLabels -join ', ')" }
            Write-Failure "Аккаунта `"$label`" нет$known. Чтобы добавить новый, вставьте в PowerShell: $(Get-InstallerCommand '-AddAccount')"
            exit 1
        }
    } elseif ((Test-LoginSupportsAccounts) -and (Read-AccountList) -and $script:AccountLabels.Count -ge 2) {
        $label = Read-AccountChoice -Question 'Аккаунтов несколько. В какой войти заново?'
    }

    $creds = Initialize-ApiCredentials
    Invoke-TelegramLogin -ApiId $creds.ApiId -ApiHash $creds.ApiHash -Account $label -Qr:$Qr -Phone:$Phone
    $script:SelfCheckAccount = $label
}

function Invoke-DefaultLogin {
    param([switch]$Qr, [switch]$Phone)

    if (Test-FullConfig -Path $ConfigPath) {
        Write-Info 'Вход в Telegram уже выполнен -- использую сохранённую сессию.'
        if ((Test-LoginSupportsAccounts) -and (Read-AccountList) -and $script:AccountLabels.Count -gt 0) {
            Write-Info "Аккаунты: $(Format-Accounts)."
            Write-Info "Добавить ещё один аккаунт, вставьте в PowerShell: $(Get-InstallerCommand '-AddAccount')"
            if ($script:AccountLabels.Count -ge 2) {
                $first = $script:AccountLabels[0]
                Write-Info "Чтобы войти заново в один из них (например, после отзыва сессии), вставьте в PowerShell: $(Get-InstallerCommand "-Relogin -Account $first") -- вместо `"$first`" подставьте нужное название."
            } else {
                Write-Info "Чтобы войти заново (например, после отзыва сессии), вставьте в PowerShell: $(Get-InstallerCommand '-Relogin')"
            }
        } else {
            Write-Info "Чтобы войти заново (например, после отзыва сессии), вставьте в PowerShell: $(Get-InstallerCommand '-Relogin')"
        }
        return
    }
    $creds = Initialize-ApiCredentials
    Invoke-TelegramLogin -ApiId $creds.ApiId -ApiHash $creds.ApiHash -Qr:$Qr -Phone:$Phone
}

# ---------------------------------------------------------------------------
# Client registration
# ---------------------------------------------------------------------------

# Claude Code: either `claude` on PATH, or the copy the Claude Desktop app
# unpacks for its own "Code" tab (not on PATH):
# %LOCALAPPDATA%\Claude-Data\claude_<publisher id>\claude-code\<version>\<hash>\claude.exe
# Both read the same ~\.claude, so registering through either is enough.
function Get-ClaudeCodeExe {
    $onPath = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }

    if (-not $env:LOCALAPPDATA) { return $null }
    $dataRoot = Join-Path $env:LOCALAPPDATA 'Claude-Data'
    if (-not (Test-Path $dataRoot)) { return $null }
    $found = Get-ChildItem -Path $dataRoot -Directory -Filter 'claude_*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'claude-code' } |
        Where-Object { Test-Path $_ } |
        ForEach-Object { Get-ChildItem -Path $_ -Recurse -Depth 2 -File -Filter 'claude.exe' -ErrorAction SilentlyContinue } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Register-ClaudeCode {
    $claudeExe = Get-ClaudeCodeExe
    if (-not $claudeExe) { return }
    Write-Info 'Обнаружен Claude Code -- регистрирую сервер...'
    Invoke-NativeQuiet -Exe $claudeExe -Arguments @('mcp', 'remove', '-s', 'user', $ServerName) | Out-Null
    $addStatus = Invoke-NativeQuiet -Exe $claudeExe -Arguments @('mcp', 'add', '-s', 'user', $ServerName, '--', (Get-VenvServerExe))
    if ($addStatus -eq 0) {
        Write-Ok 'Claude Code настроен (scope: user).'
        if ($script:AutoApproveTools) {
            $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
            Update-ClaudeSettingsPermission -SettingsPath $settingsPath -Action 'add'
        }
    } else {
        Write-Warn "Не удалось зарегистрировать сервер в Claude Code. Добавьте вручную: `"$claudeExe`" mcp add -s user $ServerName -- $(Get-VenvServerExe)"
    }
}

function Get-MaintenancePython {
    # Returns a usable python.exe path, or $null. Prefers the venv; falls
    # back to a throwaway venv built the same way the real one is.
    $venvPy = Get-VenvPython
    if (Test-Path $venvPy) { return $venvPy }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { return $null }

    $tmpRoot = Join-Path $env:TEMP ([System.Guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null
    $tmpVenv = Join-Path $tmpRoot 'venv'
    uv venv --seed --managed-python --python $PythonVersion $tmpVenv *> $null
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

# Feeds a Python script to `python -` through stdin. The top of this file sets
# $OutputEncoding to UTF-8 so Cyrillic output is readable; Windows PowerShell
# 5.1 then prefixes the piped text with a byte-order mark, and Python rejects it
# ("invalid non-printable character U+FEFF"). A BOM-less encoding in this
# function's own scope avoids that without touching the caller's.
function Invoke-PythonScript {
    param([string]$PythonExe, [string]$ScriptBody, [string[]]$Arguments)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $ScriptBody | & $PythonExe - @Arguments
}

function Invoke-PythonEdit {
    param([string]$PythonExe, [string]$ScriptBody, [string[]]$Arguments)
    Invoke-PythonScript -PythonExe $PythonExe -ScriptBody $ScriptBody -Arguments $Arguments
    return ($LASTEXITCODE -eq 0)
}

# Runs a client's CLI and returns its exit code, discarding its output. Under
# $ErrorActionPreference = 'Stop', Windows PowerShell 5.1 turns anything a
# native command writes to stderr into a terminating error -- even redirected --
# so `claude mcp remove` on a server that isn't there ("No MCP server named...")
# would abort the whole step. The preference is lowered in this function's
# scope only.
function Invoke-NativeQuiet {
    param([string]$Exe, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments *> $null
    return $LASTEXITCODE
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
        Invoke-NativeQuiet -Exe 'codex' -Arguments @('mcp', 'remove', $ServerName) | Out-Null
        $addStatus = Invoke-NativeQuiet -Exe 'codex' -Arguments @('mcp', 'add', $ServerName, '--', (Get-VenvServerExe))
        if ($addStatus -eq 0) {
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

# Where Claude Desktop keeps claude_desktop_config.json depends on how it was
# installed, so the folder is searched for, first match wins:
#   1. %LOCALAPPDATA%\Claude-Data\claude_<publisher id> -- current Microsoft
#      Store build: user data lives outside the package.
#   2. %LOCALAPPDATA%\Packages\Claude_<publisher id>\LocalCache\Roaming\Claude --
#      older Store builds, where Windows redirects %APPDATA% into the package.
#      The current build leaves a ".user-data-left.*" marker there once it has
#      moved the data out; such a folder is stale and skipped.
#   3. %APPDATA%\Claude -- the classic installer.
# The publisher id (pzs8sxrjxfjjc) is the same on every machine, but it is
# matched with a wildcard so a changed signing certificate doesn't break this.
# Returns the folder, or $null.
function Get-ClaudeDesktopDir {
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $dataRoot = Join-Path $env:LOCALAPPDATA 'Claude-Data'
        if (Test-Path $dataRoot) {
            $candidates += Get-ChildItem -Path $dataRoot -Directory -Filter 'claude_*' -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName }
        }
        $packagesRoot = Join-Path $env:LOCALAPPDATA 'Packages'
        if (Test-Path $packagesRoot) {
            $candidates += Get-ChildItem -Path $packagesRoot -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path (Join-Path (Join-Path $_.FullName 'LocalCache') 'Roaming') 'Claude' }
        }
    }
    if ($env:APPDATA) { $candidates += Join-Path $env:APPDATA 'Claude' }

    foreach ($dir in $candidates) {
        if (-not (Test-Path $dir)) { continue }
        if (Get-ChildItem -Path $dir -Force -Filter '.user-data-left*' -ErrorAction SilentlyContinue) { continue }
        return $dir
    }
    return $null
}

# A Store install whose data folder isn't there yet: the app has never been
# started, so there is nothing to register with.
function Test-ClaudeDesktopStoreInstalled {
    if (-not $env:LOCALAPPDATA) { return $false }
    $packagesRoot = Join-Path $env:LOCALAPPDATA 'Packages'
    if (-not (Test-Path $packagesRoot)) { return $false }
    return [bool](Get-ChildItem -Path $packagesRoot -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue)
}

function Register-ClaudeDesktop {
    $claudeDir = Get-ClaudeDesktopDir
    if (-not $claudeDir) {
        if (Test-ClaudeDesktopStoreInstalled) {
            Write-Warn 'Claude Desktop установлен, но папка его настроек не найдена -- похоже, приложение ещё ни разу не запускали. Откройте его один раз, полностью закройте и запустите установщик снова.'
        }
        return
    }
    $target = Join-Path $claudeDir 'claude_desktop_config.json'

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
        Write-Ok "Claude Desktop настроен ($target)."
        Write-Info 'Чтобы Claude Desktop увидел сервер, закройте его полностью -- крестик на окне приложение не закрывает: нажмите правой кнопкой на значок Claude в области уведомлений (справа внизу, рядом с часами; может быть под стрелкой ^), выберите "Выход" и откройте приложение снова. Если не получилось -- перезагрузите компьютер.'
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
    # SkillsRoot: Codex CLI and the ChatGPT desktop app read the same SKILL.md
    # format from ~\.agents\skills, so the one file is copied there too -- see
    # Install-CodexSkills.
    param([string]$Name, [string]$SkillsRoot = (Join-Path $env:USERPROFILE '.claude\skills'))

    $dest = Join-Path $SkillsRoot $Name
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
# telegram-digest/ folder, not a bare SKILL.md -- the repo carries it ready
# built, named telegram-digest-skill-<version>.zip, so nobody has to zip
# anything themselves. It goes straight to Downloads, under the same name:
# that's where README tells users to look for it, since it's a folder every
# non-developer already knows how to find. The version in the name tells
# uploads of different skill versions apart.
function Install-ClaudeSkillZip {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal installer helper, not a public cmdlet.')]
    param()

    $base = "https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/$RepoRef/.claude/skills"
    $name = 'telegram-digest-skill.zip'
    try {
        $skillMd = Invoke-RestMethod -Uri "$base/telegram-digest/SKILL.md" -ErrorAction Stop
        $versionMatch = [regex]::Match($skillMd, '(?m)^\s+version: "(\d+\.\d+)"\s*$')
        if ($versionMatch.Success) { $name = "telegram-digest-skill-$($versionMatch.Groups[1].Value).zip" }
    } catch {
        Write-Verbose "Could not read the skill version: $($_.Exception.Message)"
    }

    $downloadsDir = Join-Path $env:USERPROFILE 'Downloads'
    New-Item -ItemType Directory -Force -Path $downloadsDir | Out-Null
    $dest = Join-Path $downloadsDir $name
    try {
        try {
            Invoke-WebRequest -Uri "$base/$name" -OutFile $dest -ErrorAction Stop
        } catch {
            # Releases from before the zip carried its version in the repo have
            # it as telegram-digest.zip; the fallback can go once every release
            # has the new name.
            Invoke-WebRequest -Uri "$base/telegram-digest.zip" -OutFile $dest -ErrorAction Stop
        }
        Write-Ok "Готовый архив скилла для Claude Desktop (в Загрузках): $dest"
        Write-Info "В обычном чате Claude Desktop скиллы читаются не с диска, а из вашего аккаунта claude.ai: чтобы сводка работала и там, подключите файл $name из Загрузок через значок профиля -> Settings -> Customize -> Skills -> Add -> Upload a skill (подробности в README)."
    } catch {
        Remove-Item -Force $dest -ErrorAction SilentlyContinue
        Write-Warn 'Не удалось скачать архив скилла для Claude Desktop -- не критично, остальное работает и без него.'
    }
    # Earlier installers kept a copy here too; nothing reads it.
    Remove-Item -Force (Join-Path $InstallDir 'telegram-digest-skill.zip') -ErrorAction SilentlyContinue
}

function Install-ClaudeSkills {
    $haveCli = [bool](Get-ClaudeCodeExe)
    $haveDesktop = [bool](Get-ClaudeDesktopDir)
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
    }
}

# Codex CLI, the Codex IDE extension and the ChatGPT desktop app read personal
# skills from ~\.agents\skills (a folder with a SKILL.md), picking up changes on
# their own. ChatGPT Chat/Work on the web and mobile only see skills shipped
# inside plugins, which is out of reach here. Same detection as Register-Codex.
function Install-CodexSkills {
    $hasCodexCli = [bool](Get-Command codex -ErrorAction SilentlyContinue)
    if (-not $hasCodexCli -and -not (Test-Path (Join-Path $env:USERPROFILE '.codex'))) { return }

    Write-Info 'Устанавливаю скилл для Codex / ChatGPT Desktop (сводка по Telegram)...'
    try {
        Install-ClaudeSkill -Name 'telegram-digest' -SkillsRoot (Join-Path $env:USERPROFILE '.agents\skills')
    } catch {
        Write-Warn 'Установка скилла не удалась -- не критично, остальное работает и без него.'
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

account = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
result = asyncio.run(check_transcription_access(account=account))
try:
    data = json.loads(result)
except json.JSONDecodeError:
    data = None

print(result)
sys.exit(0 if data and "is_premium" in data else 1)
'@

# The account the self-check asks about. With several accounts the server answers
# a call without `account` with a question instead of JSON, so one has to be
# named: the one just added or signed in again, else the default, else the first.
# With a single account (or an older package) none is named.
function Get-SelfCheckAccount {
    if ($script:SelfCheckAccount) { return $script:SelfCheckAccount }
    if (-not (Test-LoginSupportsAccounts)) { return '' }
    if (-not (Read-AccountList)) { return '' }
    if ($script:AccountLabels.Count -lt 2) { return '' }
    if ($script:AccountDefault) { return $script:AccountDefault }
    return $script:AccountLabels[0]
}

function Invoke-SelfCheck {
    Write-Host ''
    $account = Get-SelfCheckAccount
    if ($account) {
        Write-Info "Проверяю подключение к Telegram (аккаунт `"$account`")..."
    } else {
        Write-Info 'Проверяю подключение к Telegram...'
    }
    $py = Get-VenvPython
    # The server logs INFO lines to stderr; with 2>&1 and 'Stop' PowerShell 5.1
    # would treat the first one as a failure.
    $ErrorActionPreference = 'Continue'
    $output = Invoke-PythonScript -PythonExe $py -ScriptBody $script:SelfCheckScript -Arguments @($account) 2>&1
    $status = $LASTEXITCODE
    $outputText = ($output | Out-String).TrimEnd()

    if ($status -eq 0) {
        Write-Ok 'Подключение к Telegram работает.'
    } else {
        Write-Warn 'Самопроверка не прошла:'
        Write-Host $outputText
        Write-Warn "Настройки сохранены, но сервер пока не отвечает как ожидалось. Запустите вручную: $(Get-VenvServerExe)"
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
        $errorLog = (Invoke-PythonScript -PythonExe $py -ScriptBody $errorLogScript 2>$null | Out-String).Trim()
    } catch {
        $errorLog = ''
    }

    Write-Host ''
    Write-Ok "Готово! telegram-mcp-ag$(Format-Version $script:InstalledVersion) установлен."
    Write-Host ''
    Write-Host 'Что дальше:'
    Write-Host '  - Claude Desktop: закройте полностью (значок в области уведомлений -> "Выход"; если не получилось -- перезагрузите компьютер) и откройте снова.'
    Write-Host '  - Claude Code и Codex: откройте новую сессию.'
    Write-Host '  - ChatGPT Desktop: перезапускать обычно не нужно -- сервер и скилл подхватятся сами в новом чате.'
    Write-Host "  - Проверить сервер вручную: $(Get-VenvServerExe)"
    if ($errorLog) {
        Write-Host "  - Лог ошибок сервера: $errorLog"
    }
    if ((Test-LoginSupportsAccounts) -and (Read-AccountList) -and $script:AccountLabels.Count -gt 0) {
        Write-Host "  - Аккаунты: $(Format-Accounts)"
        Write-Host '  - Добавить ещё один аккаунт, вставьте в PowerShell:'
        Write-Host "      $(Get-InstallerCommand '-AddAccount')"
        if ($script:AccountLabels.Count -ge 2) {
            Write-Host '  - Повторный вход в Telegram (например, после отзыва сессии), вставьте в PowerShell команду нужного аккаунта:'
            foreach ($label in $script:AccountLabels) {
                Write-Host "      $(Get-InstallerCommand "-Relogin -Account $label")"
            }
        } else {
            Write-Host '  - Повторный вход в Telegram (например, после отзыва сессии), вставьте в PowerShell:'
            Write-Host "      $(Get-InstallerCommand '-Relogin')"
        }
    } else {
        Write-Host '  - Повторный вход в Telegram (например, после отзыва сессии), вставьте в PowerShell:'
        Write-Host "      $(Get-InstallerCommand '-Relogin')"
    }
    Write-Host '  - Полное удаление, вставьте в PowerShell:'
    Write-Host "      $(Get-InstallerCommand '-Uninstall')"
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
    Wait-InstallFree
    Write-Info 'Снимаю регистрации из клиентов...'

    $claudeExe = Get-ClaudeCodeExe
    if ($claudeExe) {
        $removeStatus = Invoke-NativeQuiet -Exe $claudeExe -Arguments @('mcp', 'remove', '-s', 'user', $ServerName)
        if ($removeStatus -eq 0) { Write-Ok 'Claude Code: сервер удалён.' }
        $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
        Update-ClaudeSettingsPermission -SettingsPath $settingsPath -Action 'remove'
    }

    foreach ($name in @('telegram-digest', 'setup-telegram-mcp')) {
        $skillDir = Join-Path $env:USERPROFILE ".claude\skills\$name"
        if (Test-Path $skillDir) { Remove-Item -Recurse -Force $skillDir }
    }

    $agentsSkillDir = Join-Path $env:USERPROFILE '.agents\skills\telegram-digest'
    if (Test-Path $agentsSkillDir) { Remove-Item -Recurse -Force $agentsSkillDir }

    $codexConfig = Join-Path $env:USERPROFILE '.codex\config.toml'
    if (Get-Command codex -ErrorAction SilentlyContinue) {
        $removeStatus = Invoke-NativeQuiet -Exe 'codex' -Arguments @('mcp', 'remove', $ServerName)
        if ($removeStatus -eq 0) {
            Write-Ok 'Codex: сервер удалён.'
        } else {
            Remove-CodexTomlEntry -CodexConfig $codexConfig
        }
    } else {
        Remove-CodexTomlEntry -CodexConfig $codexConfig
    }

    $claudeDesktopDir = Get-ClaudeDesktopDir
    if ($claudeDesktopDir) {
        Remove-ClaudeDesktopJsonEntry -Target (Join-Path $claudeDesktopDir 'claude_desktop_config.json')
    }

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

# Checks the combination of flags and explains what is wrong; exits if it is.
function Assert-InstallerArguments {
    param([switch]$Uninstall, [switch]$Relogin, [switch]$AddAccount, [string]$Account)

    if ($Account -and $Account -notmatch '^[A-Za-z0-9_-]{1,32}$') {
        Write-Failure 'Название аккаунта -- латинские буквы, цифры, "_" и "-", не длиннее 32 символов. Например, "рабочий" -> work.'
        exit 1
    }
    if ($Account -and -not $Relogin) {
        Write-Failure '-Account работает только вместе с -Relogin. Чтобы добавить новый аккаунт, используйте -AddAccount.'
        exit 1
    }
    if (([int][bool]$Uninstall + [int][bool]$Relogin + [int][bool]$AddAccount) -gt 1) {
        Write-Failure 'Флаги -Uninstall, -Relogin и -AddAccount нельзя сочетать: выберите один.'
        exit 1
    }
}

function Invoke-Main {
    param([switch]$Uninstall, [switch]$Relogin, [switch]$AddAccount, [string]$Account, [switch]$Qr, [switch]$Phone)

    Test-SupportedPlatform
    Assert-InstallerArguments -Uninstall:$Uninstall -Relogin:$Relogin -AddAccount:$AddAccount -Account $Account

    if ($Uninstall) {
        Invoke-Uninstall
        return
    }

    Wait-InstallFree
    Initialize-PythonAndVenv
    Install-ServerPackage
    Confirm-NewVenv

    if ($AddAccount) {
        Invoke-AddAccount -Qr:$Qr -Phone:$Phone
    } elseif ($Relogin) {
        Invoke-ReloginAccount -Account $Account -Qr:$Qr -Phone:$Phone
    } else {
        Invoke-DefaultLogin -Qr:$Qr -Phone:$Phone
    }

    # Each step degrades independently: a client that isn't installed, or
    # an unexpected error registering one of them, must not stop the rest
    # from being tried -- mirrors install.sh's per-client resilience.
    foreach ($step in @('Register-ClaudeCode', 'Register-Codex', 'Register-ClaudeDesktop', 'Install-ClaudeSkills', 'Install-CodexSkills', 'Invoke-SelfCheck')) {
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
        Invoke-Main -Uninstall:$Uninstall -Relogin:$Relogin -AddAccount:$AddAccount -Account $Account -Qr:$Qr -Phone:$Phone
    } finally {
        Restore-VenvBackupOnFailure
        Remove-MaintenanceVenvs
    }
}
