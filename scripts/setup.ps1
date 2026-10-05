# Requires PowerShell 7+, Python 3.11–3.13, uv, and Node.js 22+.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
Invoke-Checked 'uv' @('sync', '--locked')
Push-Location 'frontend'
try {
    Invoke-Checked 'npm' @('ci')
    Invoke-Checked 'npm' @('run', 'build')
} finally { Pop-Location }
Invoke-Checked 'uv' @('run', 'telegram-search', 'setup')
Invoke-Checked 'uv' @('run', 'telegram-search', 'doctor')
