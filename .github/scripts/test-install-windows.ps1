# Runs the real install steps of install.ps1 under Windows PowerShell 5.1, in a
# profile whose path has a space and Cyrillic letters, with git hidden and uv
# not yet installed. Then starts the installed server. Stops short of the
# Telegram login and client registration, which need a person.
#
# The repository needs TELEGRAM_MCP_AG_REF set to a pushed commit: the installer
# downloads that commit's archive and pyproject.toml from GitHub.
$ErrorActionPreference = 'Stop'

$env:Path = ($env:Path -split ';' | Where-Object { $_ -notmatch '\\Git\\' }) -join ';'
if (Get-Command git -ErrorAction SilentlyContinue) {
    throw 'git is still on PATH, this run would not prove anything'
}

# "Тест Пользователь", spelled with escapes so this file stays plain ASCII.
$profileName = -join ([char[]](0x0422, 0x0435, 0x0441, 0x0442, 0x20, 0x041F, 0x043E, 0x043B, 0x044C, 0x0437, 0x043E, 0x0432, 0x0430, 0x0442, 0x0435, 0x043B, 0x044C))
$env:USERPROFILE = Join-Path 'C:\Users' $profileName
New-Item -ItemType Directory -Force -Path $env:USERPROFILE | Out-Null

# Dot-sourcing defines everything and skips Invoke-Main.
. (Join-Path $PSScriptRoot '..\..\install.ps1')

Initialize-PythonAndVenv
Install-ServerPackage
Confirm-NewVenv

if (-not (Test-Path (Join-Path $script:UpstreamSource 'pyproject.toml'))) {
    throw 'upstream source directory is missing'
}
if (Test-Path $VenvBackupDir) { throw 'venv backup was not removed' }

$py = Get-VenvPython
$version = & $py --version
Write-Host $version
if ($version -notmatch "^Python $([regex]::Escape($PythonVersion))\.") { throw "expected Python $PythonVersion, got: $version" }
# The same call the client-config helpers make.
& $py -m pip install -q tomlkit
if ($LASTEXITCODE -ne 0) { throw 'pip cannot install: the client-config helpers need it' }

& $py (Join-Path $PSScriptRoot 'probe_server.py') (Get-VenvServerExe)
if ($LASTEXITCODE -ne 0) { throw 'the installed server did not start' }

Write-Host 'Second run: the upstream copy must be reused and the server must still start.'
Initialize-PythonAndVenv
Install-ServerPackage
Confirm-NewVenv
& $py (Join-Path $PSScriptRoot 'probe_server.py') (Get-VenvServerExe)
if ($LASTEXITCODE -ne 0) { throw 'the reinstalled server did not start' }
