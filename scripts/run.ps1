# Requires PowerShell 7+.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
& uv run --locked telegram-search run @args
exit $LASTEXITCODE
