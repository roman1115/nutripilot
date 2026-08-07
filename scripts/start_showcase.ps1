param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env. Copy .env.showcase.example to .env and set real credentials."
}

$envText = Get-Content -LiteralPath $envFile -Raw
if ($envText -match "replace-with-your-dashscope-key" -or $envText -notmatch "(?m)^MODEL_API_KEY=.+") {
    throw "MODEL_API_KEY is not configured with a real DashScope key."
}
if ($envText -match "replace-with-a-random-internal-token") {
    throw "MCP_INTERNAL_TOKEN still contains the placeholder value."
}

$docker = Get-Command docker -ErrorAction Stop
Push-Location $projectRoot
try {
    $composeArgs = @("compose", "-f", "docker-compose.yml", "-f", "docker-compose.showcase.yml", "up")
    if ($SkipBuild) {
        $composeArgs += "--no-build"
    }
    else {
        $composeArgs += "--build"
    }
    $composeArgs += "-d"

    & $docker.Source @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to start the showcase stack."
    }

    foreach ($attempt in 1..120) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health/ready" -TimeoutSec 2
            if (
                $health.model_provider -ne "fake" -and
                $health.embedding_provider -eq "bge" -and
                $health.rerank_provider -eq "bge" -and
                $health.storage -eq "postgresql" -and
                $health.mcp_transport -eq "streamable-http"
            ) {
                & (Join-Path $projectRoot ".venv\Scripts\python.exe") scripts\verify_showcase.py
                Write-Host "NutriPilot real-model showcase is ready at http://localhost:5173"
                exit 0
            }
        }
        catch {
            # Services and local BGE model loading may need several minutes on first start.
        }
        Start-Sleep -Seconds 2
    }
    throw "Showcase services started but did not pass readiness checks."
}
finally {
    Pop-Location
}
