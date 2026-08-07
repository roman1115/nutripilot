$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDirectory = Join-Path $projectRoot ".runtime"

function Get-DescendantProcessIds([int]$ParentId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Get-DescendantProcessIds -ParentId $child.ProcessId
        $child.ProcessId
    }
}

foreach ($name in "api", "web") {
    $pidFile = Join-Path $runtimeDirectory "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }
    $savedProcessId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$savedProcessId" -ErrorAction SilentlyContinue
    $expectedCommand = if ($name -eq "api") {
        "*uvicorn*apps.api.main:app*"
    }
    else {
        "*npm*--prefix*web*run*dev*"
    }
    if ($process -and $process.CommandLine -like $expectedCommand) {
        $descendantIds = @(Get-DescendantProcessIds -ParentId $savedProcessId)
        if ($descendantIds.Count -gt 0) {
            Stop-Process -Id $descendantIds -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $savedProcessId -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force
}

Write-Host "NutriPilot demo processes started by the script have been stopped."
