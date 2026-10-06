# Requires PowerShell 7+.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
& uv run --locked --extra semantic telegram-search run @args
exit $LASTEXITCODE
