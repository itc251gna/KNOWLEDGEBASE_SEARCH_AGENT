Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location -Path (Split-Path -Parent $PSScriptRoot)
docker compose run --rm app python -m portal_search_agent.cli crawl --reset

