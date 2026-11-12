$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir
& "$ProjectDir\.venv\Scripts\python.exe" scripts/demo_launcher.py --mode real --foreground @args
exit $LASTEXITCODE
