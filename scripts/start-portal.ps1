Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $projectRoot

if (!(Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

docker compose up -d --build opensearch tika app
Start-Sleep -Seconds 3
Start-Process "http://localhost:8080/admin"
