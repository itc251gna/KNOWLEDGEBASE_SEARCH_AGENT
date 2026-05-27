param(
    [string]$BaseUrl = "http://localhost:8080",
    [string]$Username = $env:ADMIN_USERNAME,
    [string]$Password = $env:ADMIN_PASSWORD
)

$ErrorActionPreference = "Stop"

function Read-DotEnvValue {
    param([string]$Name)
    $envPath = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
    if (-not (Test-Path $envPath)) { return $null }
    $line = Get-Content $envPath | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -replace "^\s*$Name\s*=\s*", "").Trim().Trim('"').Trim("'")
}

if (-not $Username) { $Username = Read-DotEnvValue "ADMIN_USERNAME" }
if (-not $Password) { $Password = Read-DotEnvValue "ADMIN_PASSWORD" }
if (-not $Username) { $Username = "admin" }
if (-not $Password) { $Password = "local-admin-change-me" }

$BaseUrl = $BaseUrl.TrimEnd("/")
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param([string]$Name, [bool]$Passed, [string]$Detail = "")
    $results.Add([pscustomobject]@{
        Check = $Name
        Result = if ($Passed) { "PASS" } else { "FAIL" }
        Detail = $Detail
    })
}

function Invoke-Json {
    param(
        [string]$Method = "GET",
        [string]$Path,
        [object]$Body = $null,
        [Microsoft.PowerShell.Commands.WebRequestSession]$WebSession = $session,
        [int[]]$ExpectedStatus = @(200)
    )
    $params = @{
        Method = $Method
        Uri = "$BaseUrl$Path"
        WebSession = $WebSession
        Headers = @{ "Accept" = "application/json" }
    }
    if ($null -ne $Body) {
        $params.ContentType = "application/json"
        $params.Body = ($Body | ConvertTo-Json -Depth 8)
    }
    try {
        $response = Invoke-WebRequest @params
        if ($ExpectedStatus -notcontains [int]$response.StatusCode) {
            throw "Expected $($ExpectedStatus -join ',') but got $($response.StatusCode)"
        }
        return $response
    } catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($ExpectedStatus -contains [int]$statusCode) {
            return $_.Exception.Response
        }
        throw
    }
}

function Wait-CrawlIdle {
    param([int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $statusResponse = Invoke-Json -Path "/api/crawl/status"
        $statusJson = $statusResponse.Content | ConvertFrom-Json
        if (-not $statusJson.running) { return $statusJson }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Crawler did not become idle within $TimeoutSeconds seconds."
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$fixtureDir = Join-Path $repoRoot "data\ui-test-kb-source"
$fixtureMarker = "ui_smoke_marker_" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$fixtureFile = Join-Path $fixtureDir "ui-test-knowledge-source.txt"
New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
Set-Content -Path $fixtureFile -Encoding UTF8 -Value @"
Portal Search UI smoke fixture.
Marker: $fixtureMarker
This file validates local folder knowledge-source configuration and filesystem-only build.
"@

$adminHtml = Invoke-WebRequest -Uri "$BaseUrl/admin" -WebSession $session
Add-Result "Admin page loads" ($adminHtml.StatusCode -eq 200) "HTTP $($adminHtml.StatusCode)"
foreach ($id in @("adminLoginForm", "startIncremental", "startFull", "stopCrawl", "refreshDiagnostics", "createHealthReport", "requeueStatus", "saveSynonym", "kbSourceSummary", "kbBuildStatus", "kbSourceFormStatus", "kbSourceDialogStatus", "kbBackupStatus", "metricTarget", "metricSource", "openKbSourceDialog", "kbSourceDialog", "saveKbSource", "createKbBackup")) {
    Add-Result "Admin UI contains #$id" ($adminHtml.Content -match "id=`"$id`"")
}
foreach ($kind in @("web", "portal", "folder", "file", "database")) {
    Add-Result "Source modal contains kind: $kind" ($adminHtml.Content -match "value=`"$kind`"")
}
Add-Result "Admin UI contains source selector" ($adminHtml.Content -match "data-search-source-scopes")
Add-Result "Admin UI contains build source selector" ($adminHtml.Content -match "data-build-source")
foreach ($tab in @("search", "crawler", "diagnostics", "tuning", "integration")) {
    Add-Result "Admin tab exists: $tab" ($adminHtml.Content -match "data-tab=`"$tab`"")
}

$publicHtml = Invoke-WebRequest -Uri "$BaseUrl/" -WebSession $session
Add-Result "Public search page loads" ($publicHtml.StatusCode -eq 200) "HTTP $($publicHtml.StatusCode)"
$embedHtml = Invoke-WebRequest -Uri "$BaseUrl/embed" -WebSession $session
Add-Result "Embed search page loads" ($embedHtml.StatusCode -eq 200) "HTTP $($embedHtml.StatusCode)"

$crawlStatus = Invoke-Json -Path "/api/crawl/status"
Add-Result "Crawler status is public read-only" ($crawlStatus.StatusCode -eq 200)

$unauthSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$blocked = Invoke-Json -Path "/api/admin/diagnostics" -WebSession $unauthSession -ExpectedStatus @(401)
Add-Result "Diagnostics blocks anonymous access" ($blocked.StatusCode.value__ -eq 401 -or $blocked.StatusCode -eq 401)

$login = Invoke-Json -Method "POST" -Path "/api/admin/login" -Body @{ username = $Username; password = $Password }
Add-Result "Admin login succeeds" ($login.StatusCode -eq 200)

$sessionCheck = Invoke-Json -Path "/api/admin/session"
$sessionJson = $sessionCheck.Content | ConvertFrom-Json
Add-Result "Admin session becomes authenticated" ($sessionJson.authenticated -eq $true) "user=$($sessionJson.username)"

$diagnostics = Invoke-Json -Path "/api/admin/diagnostics"
Add-Result "Diagnostics loads after login" ($diagnostics.StatusCode -eq 200)

$kbSources = Invoke-Json -Path "/api/kb/sources"
Add-Result "Knowledge build sources load after login" ($kbSources.StatusCode -eq 200)

$emptyBuild = Invoke-Json -Method "POST" -Path "/api/kb/build" -Body @{ source_ids = @(); reset = $false; recreate_index = $false } -ExpectedStatus @(400)
Add-Result "Knowledge build requires selected source" ($emptyBuild.StatusCode.value__ -eq 400 -or $emptyBuild.StatusCode -eq 400)

$testSource = Invoke-Json -Method "POST" -Path "/api/kb/sources" -Body @{ type = "web"; name = "Smoke Test Source"; location = "http://localhost:8080/smoke-source"; enabled = $true }
$testSourceJson = $testSource.Content | ConvertFrom-Json
Add-Result "Add custom web source works" ($testSource.StatusCode -eq 200) $testSourceJson.id
$deleteSource = Invoke-Json -Method "DELETE" -Path "/api/kb/sources/$($testSourceJson.id)"
Add-Result "Delete custom source works" ($deleteSource.StatusCode -eq 200) $testSourceJson.id

$dbSource = Invoke-Json -Method "POST" -Path "/api/kb/sources" -Body @{ type = "database"; name = "Smoke Test Database"; location = "sqlite:///app/data/smoke-test.sqlite"; enabled = $true }
$dbSourceJson = $dbSource.Content | ConvertFrom-Json
Add-Result "Add database source registration works" ($dbSource.StatusCode -eq 200) $dbSourceJson.id
$deleteDbSource = Invoke-Json -Method "DELETE" -Path "/api/kb/sources/$($dbSourceJson.id)"
Add-Result "Delete database source registration works" ($deleteDbSource.StatusCode -eq 200) $dbSourceJson.id

$fileSource = Invoke-Json -Method "POST" -Path "/api/kb/sources" -Body @{ type = "file"; name = "Smoke Test Single File"; location = "/app/data/ui-test-kb-source/ui-test-knowledge-source.txt"; enabled = $true }
$fileSourceJson = $fileSource.Content | ConvertFrom-Json
Add-Result "Add single file source works" ($fileSource.StatusCode -eq 200) $fileSourceJson.id
$deleteFileSource = Invoke-Json -Method "DELETE" -Path "/api/kb/sources/$($fileSourceJson.id)"
Add-Result "Delete single file source works" ($deleteFileSource.StatusCode -eq 200) $fileSourceJson.id

$filesystemSource = Invoke-Json -Method "POST" -Path "/api/kb/sources" -Body @{ type = "filesystem"; name = "UI Test Local Folder"; location = "/app/data/ui-test-kb-source"; enabled = $true }
$filesystemSourceJson = $filesystemSource.Content | ConvertFrom-Json
Add-Result "Add local folder source works" ($filesystemSource.StatusCode -eq 200) $filesystemSourceJson.id

$beforeLocalBuild = Invoke-Json -Path "/api/crawl/status"
$beforeLocalBuildJson = $beforeLocalBuild.Content | ConvertFrom-Json
if ($beforeLocalBuildJson.running) {
    Add-Result "Filesystem-only build skipped when crawler busy" $true "Existing build is running."
} else {
    $localBuild = Invoke-Json -Method "POST" -Path "/api/kb/build" -Body @{ source_ids = @($filesystemSourceJson.id); reset = $false; recreate_index = $false }
    Add-Result "Filesystem-only knowledge build starts" ($localBuild.StatusCode -eq 200) $filesystemSourceJson.id
    $idleStatus = Wait-CrawlIdle -TimeoutSeconds 120
    Add-Result "Filesystem-only knowledge build finishes" (-not $idleStatus.running -and [string]::IsNullOrWhiteSpace($idleStatus.last_error)) $idleStatus.last_message
    $fixtureSearch = Invoke-Json -Path "/api/search?q=$fixtureMarker&size=3&source=filesystem"
    $fixtureSearchJson = $fixtureSearch.Content | ConvertFrom-Json
    Add-Result "Filesystem build indexes local folder document" ($fixtureSearch.StatusCode -eq 200 -and $fixtureSearchJson.total -ge 1) "marker=$fixtureMarker"
}

$deleteFilesystemSource = Invoke-Json -Method "DELETE" -Path "/api/kb/sources/$($filesystemSourceJson.id)"
Add-Result "Delete local folder source works" ($deleteFilesystemSource.StatusCode -eq 200) $filesystemSourceJson.id

$backup = Invoke-Json -Method "POST" -Path "/api/kb/backup"
Add-Result "Knowledge backup works" ($backup.StatusCode -eq 200)

$synonyms = Invoke-Json -Path "/api/admin/synonyms"
Add-Result "Synonym list loads after login" ($synonyms.StatusCode -eq 200)

$term = "ui_smoke_test_" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$saveSynonym = Invoke-Json -Method "POST" -Path "/api/admin/synonyms" -Body @{ term = $term; variants = @("variant-a", "variant-b") }
Add-Result "Save synonym button path works" ($saveSynonym.StatusCode -eq 200) $term
$deleteSynonym = Invoke-Json -Method "DELETE" -Path "/api/admin/synonyms/$term"
Add-Result "Delete synonym button path works" ($deleteSynonym.StatusCode -eq 200) $term

$report = Invoke-Json -Method "POST" -Path "/api/admin/health-report"
Add-Result "Create health report works" ($report.StatusCode -eq 200)

$badRequeue = Invoke-Json -Method "POST" -Path "/api/crawl/requeue" -Body @{ mode = "url"; url = "https://example.invalid/not-inside-portal" } -ExpectedStatus @(400)
Add-Result "Requeue URL validates portal scope" ($badRequeue.StatusCode.value__ -eq 400 -or $badRequeue.StatusCode -eq 400)

$search = Invoke-Json -Path "/api/search?q=%CE%94%CE%9D%CE%A5&size=3"
Add-Result "Public search endpoint works" ($search.StatusCode -eq 200)

$portalSearch = Invoke-Json -Path "/api/search?q=%CE%94%CE%9D%CE%A5&size=3&source=portal"
Add-Result "Portal-only source search works" ($portalSearch.StatusCode -eq 200)

$allSearch = Invoke-Json -Path "/api/search?q=%CE%94%CE%9D%CE%A5&size=3&source=all"
Add-Result "All-source search works" ($allSearch.StatusCode -eq 200)

$missingFile = Invoke-Json -Path "/api/files/not-indexed" -ExpectedStatus @(404)
Add-Result "File endpoint hides unknown files" ($missingFile.StatusCode.value__ -eq 404 -or $missingFile.StatusCode -eq 404)

$suggest = Invoke-Json -Path "/api/suggest?q=%CE%94%CE%9D%CE%A5"
Add-Result "Public suggestions endpoint works" ($suggest.StatusCode -eq 200)

$logout = Invoke-Json -Method "POST" -Path "/api/admin/logout"
Add-Result "Admin logout succeeds" ($logout.StatusCode -eq 200)

$afterLogout = Invoke-Json -Path "/api/admin/session"
$afterLogoutJson = $afterLogout.Content | ConvertFrom-Json
Add-Result "Admin session clears after logout" ($afterLogoutJson.authenticated -eq $false)

$failed = $results | Where-Object { $_.Result -ne "PASS" }
$results | Format-Table -AutoSize
if ($failed) {
    throw "$($failed.Count) smoke check(s) failed."
}
