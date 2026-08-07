$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$docker = Get-Command docker -ErrorAction Stop

Push-Location $projectRoot
try {
    & $docker.Source compose -f docker-compose.yml -f docker-compose.showcase.yml down
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to stop the showcase stack."
    }
}
finally {
    Pop-Location
}
