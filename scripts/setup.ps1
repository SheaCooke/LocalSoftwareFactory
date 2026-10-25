<#
.SYNOPSIS
    One-time setup for the software factory on Windows 11.
.DESCRIPTION
    Checks Python 3.12+, installs uv if missing, syncs dependencies into .venv,
    creates .env from .env.example, and warns about long paths and Docker.
.PARAMETER WithKeyring
    Also install the optional keyring extra (Windows Credential Manager support).
#>
[CmdletBinding()]
param([switch]$WithKeyring)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
# Trust the Windows certificate store (corporate proxies / antivirus TLS inspection).
if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = 'true' }

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "WARNING: $msg" -ForegroundColor Yellow }

# --- Python -----------------------------------------------------------------
Write-Step 'Checking Python'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3.12+ is required: https://www.python.org/downloads/windows/' }
$ver = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
if ([version]$ver -lt [version]'3.12') { throw "Python 3.12+ is required (found $ver)" }
Write-Host "    Python $ver"

# --- uv ---------------------------------------------------------------------
Write-Step 'Checking uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '    uv not found; installing with pip --user'
    & python -m pip install --user --quiet uv
    $userScripts = & python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
    if ($env:Path -notlike "*$userScripts*") { $env:Path = "$userScripts;$env:Path" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv installed but not on PATH. Add '$userScripts' to PATH, or install uv from https://docs.astral.sh/uv/"
    }
}
Write-Host "    $(uv --version)"

# --- Dependencies -----------------------------------------------------------
Write-Step 'Installing dependencies into .venv'
$syncArgs = @('sync')
if ($WithKeyring) { $syncArgs += @('--extra', 'keyring') }
& uv @syncArgs
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }

# --- .env -------------------------------------------------------------------
if (-not (Test-Path (Join-Path $Root '.env'))) {
    Write-Step 'Creating .env from .env.example (fill in the keys you need)'
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
}

# --- Long paths -------------------------------------------------------------
Write-Step 'Checking Windows long path support'
$lp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue
if (-not $lp -or $lp.LongPathsEnabled -ne 1) {
    Write-Warn 'LongPathsEnabled is off. Deep worktrees under workspaces/ may fail.'
    Write-Host  '         Enable it (admin PowerShell), then sign out and back in:'
    Write-Host  "         New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force"
}
if (Get-Command git -ErrorAction SilentlyContinue) {
    if ((& git config --global --get core.longpaths) -ne 'true') {
        Write-Warn "git core.longpaths is not set. Recommended: git config --global core.longpaths true"
    }
} else {
    Write-Warn 'git not found on PATH. It is required for worktree isolation.'
}

# --- Docker -----------------------------------------------------------------
Write-Step 'Checking Docker (optional)'
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    & docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host '    Docker is running: sandboxed execution available.' }
    else { Write-Warn 'Docker is installed but not running. The factory will use reduced isolation.' }
} else {
    Write-Warn 'Docker not found. The factory will use the restricted-subprocess sandbox (reduced isolation).'
}

Write-Step 'Done. Try:  uv run factory prompt "hello" --model echo'
