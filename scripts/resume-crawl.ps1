param(
    [string]$BaseUrl = "http://localhost:8080",
    [string]$AdminToken = "local-admin-change-me"
)

$ErrorActionPreference = "Stop"

Invoke-RestMethod `
    -Uri "$BaseUrl/api/crawl/start" `
    -Method Post `
    -Headers @{ "X-Admin-Token" = $AdminToken } |
    ConvertTo-Json -Depth 6
