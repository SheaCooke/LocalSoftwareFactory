<#
.SYNOPSIS
    Run a factory CLI command through uv, e.g.  .\scripts\run.ps1 models list
.DESCRIPTION
    With no arguments, runs `factory up` (orchestrator + web UI).
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$env:FACTORY_ROOT = $Root
if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = 'true' }

$cmd = if ($args.Count -eq 0) { @('up') } else { $args }
& uv run --project $Root factory @cmd
exit $LASTEXITCODE
