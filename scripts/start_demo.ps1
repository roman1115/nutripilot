$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

& (Join-Path $PSScriptRoot "stop_demo.ps1")
& $python (Join-Path $PSScriptRoot "demo_launcher.py")
