param(
    [string]$PortalUrl = "http://251gna/wp_root",
    [string]$StatusUrl = "http://localhost:8080/api/crawl/status",
    [string]$StopUrl = "http://localhost:8080/api/crawl/stop",
    [string]$AdminToken = "local-admin-change-me",
    [string]$LogPath = "",
    [int]$IntervalSeconds = 15,
    [int]$TimeoutSeconds = 20,
    [int]$SlowThresholdMs = 8000,
    [int]$MaxBadStreak = 3,
    [switch]$StopOnThreshold
)

$ErrorActionPreference = "Continue"

if (-not $LogPath) {
    $root = Resolve-Path (Join-Path $PSScriptRoot "..")
    $LogPath = Join-Path $root "data\monitoring\portal-watch.csv"
}

$logDir = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not (Test-Path $LogPath)) {
    "time_local,ok,latency_ms,http_status,crawl_running,progress_percent,queued,processing,done,failed,documents,bad_streak,action,error" |
        Out-File -FilePath $LogPath -Encoding utf8
}

$badStreak = 0

while ($true) {
    $time = Get-Date -Format o
    $ok = $false
    $latencyMs = ""
    $httpStatus = ""
    $errorText = ""
    $action = ""

    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $response = Invoke-WebRequest -Uri $PortalUrl -MaximumRedirection 5 -TimeoutSec $TimeoutSeconds -UseBasicParsing
        $sw.Stop()
        $ok = $true
        $latencyMs = [math]::Round($sw.Elapsed.TotalMilliseconds)
        $httpStatus = [int]$response.StatusCode
    } catch {
        $errorText = ($_.Exception.Message -replace '"', "'")
    }

    $crawlRunning = ""
    $progress = ""
    $queued = ""
    $processing = ""
    $done = ""
    $failed = ""
    $documents = ""

    try {
        $status = Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 10
        $crawlRunning = [string]$status.running
        $progress = $status.progress_percent
        $queued = $status.stats.queued
        $processing = $status.stats.processing
        $done = $status.stats.done
        $failed = $status.stats.failed
        $documents = $status.stats.documents
    } catch {
        $errorText = (($errorText + " status: " + $_.Exception.Message).Trim() -replace '"', "'")
    }

    if ((-not $ok) -or (($latencyMs -ne "") -and ([int]$latencyMs -gt $SlowThresholdMs))) {
        $badStreak += 1
    } else {
        $badStreak = 0
    }

    if ($StopOnThreshold -and $badStreak -ge $MaxBadStreak) {
        try {
            Invoke-RestMethod -Uri $StopUrl -Method Post -Headers @{ "X-Admin-Token" = $AdminToken } -TimeoutSec 10 | Out-Null
            $action = "stop_requested"
        } catch {
            $action = "stop_failed"
            $errorText = (($errorText + " stop: " + $_.Exception.Message).Trim() -replace '"', "'")
        }
    }

    '"' + $time + '",' + $ok + ',' + $latencyMs + ',' + $httpStatus + ',' + $crawlRunning + ',' +
        $progress + ',' + $queued + ',' + $processing + ',' + $done + ',' + $failed + ',' + $documents + ',' +
        $badStreak + ',"' + $action + '","' + $errorText + '"' |
        Out-File -FilePath $LogPath -Encoding utf8 -Append

    if (($crawlRunning -eq "False") -or ($action -eq "stop_requested")) {
        break
    }

    Start-Sleep -Seconds $IntervalSeconds
}
