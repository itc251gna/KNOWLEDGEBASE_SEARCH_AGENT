param(
    [string]$Commit = "",
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$Server = "kmh251@10.4.51.232",
    [string]$SshKey = "",
    [string]$RemotePath = "/home/kmh251/deployment/portal-search-agent",
    [string]$BaseUrl = "https://knowledgebase-search.251gh.local",
    [switch]$BuildTika,
    [switch]$SkipSmokeTest,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $projectRoot

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed:`n$($output | Out-String)"
    }

    return (($output | Out-String).Trim())
}

function Assert-GitSuccess {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    if ($exitCode -ne 0) {
        throw "$FailureMessage`n$($output | Out-String)"
    }
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    if ($exitCode -ne 0) {
        throw "$FilePath $($Arguments -join ' ') failed with exit code $exitCode."
    }
}

function ConvertTo-ShellLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return "'" + ($Value -replace "'", "'\''") + "'"
}

Write-Host "Checking local production deploy preconditions..."
Invoke-Git -Arguments @("fetch", $Remote, $Branch) | Out-Null

$currentBranch = Invoke-Git -Arguments @("rev-parse", "--abbrev-ref", "HEAD")
if ($currentBranch -ne $Branch) {
    throw "Production deploy is allowed only from local branch '$Branch'. Current branch: '$currentBranch'."
}

$dirty = Invoke-Git -Arguments @("status", "--porcelain=v1")
if ($dirty) {
    throw "Production deploy blocked because the local workspace is not clean:`n$dirty"
}

$remoteRef = "$Remote/$Branch"
$remoteCommit = Invoke-Git -Arguments @("rev-parse", "$remoteRef^{commit}")

if ([string]::IsNullOrWhiteSpace($Commit)) {
    $deployCommit = Invoke-Git -Arguments @("rev-parse", "HEAD")
    if ($deployCommit -ne $remoteCommit) {
        throw "Production deploy blocked because local HEAD is not exactly $remoteRef. Pull/rebase first, or pass -Commit with a commit that exists on $remoteRef."
    }
} else {
    $deployCommit = Invoke-Git -Arguments @("rev-parse", "$Commit^{commit}")
    Assert-GitSuccess `
        -Arguments @("merge-base", "--is-ancestor", $deployCommit, $remoteRef) `
        -FailureMessage "Production deploy blocked because commit $deployCommit is not reachable from $remoteRef."
}

Write-Host "Deploy commit: $deployCommit"

$shortCommit = $deployCommit.Substring(0, 12)
$bundleRef = "refs/portal-search-deploy/$shortCommit"
$remoteBundlePath = "/tmp/portal-search-agent-$shortCommit.bundle"
$remoteScriptPath = "/tmp/portal-search-agent-deploy-$shortCommit.sh"
$buildServices = "app"
if ($BuildTika) {
    $buildServices = "app tika"
}

$quotedRemotePath = ConvertTo-ShellLiteral -Value $RemotePath
$quotedDeployCommit = ConvertTo-ShellLiteral -Value $deployCommit
$quotedBundleRef = ConvertTo-ShellLiteral -Value $bundleRef
$quotedRemoteBundlePath = ConvertTo-ShellLiteral -Value $remoteBundlePath
$quotedRemoteScriptPath = ConvertTo-ShellLiteral -Value $remoteScriptPath

$remoteScript = @"
set -eu
trap 'rm -f $remoteBundlePath $remoteScriptPath' EXIT

cd $quotedRemotePath

echo "Checking production checkout..."
if [ -n "`$(git status --porcelain=v1)" ]; then
    echo "Production deploy blocked because the production checkout is not clean:" >&2
    git status --short >&2
    exit 20
fi

git fetch $quotedRemoteBundlePath $quotedBundleRef
git checkout --detach FETCH_HEAD
actual_commit="`$(git rev-parse HEAD)"
if [ "`$actual_commit" != $quotedDeployCommit ]; then
    echo "Production deploy blocked because checkout resolved to `$actual_commit instead of $deployCommit." >&2
    exit 21
fi

docker compose -f docker-compose.prod.yml config --quiet
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 docker compose -f docker-compose.prod.yml build --pull=false $buildServices
docker compose -f docker-compose.prod.yml up -d --no-build opensearch tika app
docker compose -f docker-compose.prod.yml ps
"@

if ($DryRun) {
    Write-Host "Dry run only. A git bundle for the commit would be uploaded to $remoteBundlePath."
    Write-Host "Remote script that would be executed:"
    Write-Host $remoteScript
    exit 0
}

$tempBundle = Join-Path ([System.IO.Path]::GetTempPath()) "portal-search-agent-$shortCommit.bundle"
$tempScript = Join-Path ([System.IO.Path]::GetTempPath()) "portal-search-agent-deploy-$shortCommit.sh"

try {
    if (Test-Path $tempBundle) {
        Remove-Item $tempBundle -Force
    }
    if (Test-Path $tempScript) {
        Remove-Item $tempScript -Force
    }

    Invoke-Git -Arguments @("update-ref", $bundleRef, $deployCommit) | Out-Null
    Invoke-Git -Arguments @("bundle", "create", $tempBundle, $bundleRef) | Out-Null
    [System.IO.File]::WriteAllText($tempScript, ($remoteScript -replace "`r`n", "`n"), [System.Text.Encoding]::ASCII)

    $scpArgs = @()
    $sshArgs = @()
    if (![string]::IsNullOrWhiteSpace($SshKey)) {
        $scpArgs += @("-i", $SshKey)
        $sshArgs += @("-i", $SshKey)
    }
    $scpArgs += @("-o", "StrictHostKeyChecking=no")
    $sshArgs += @("-o", "StrictHostKeyChecking=no")

    Write-Host "Uploading deploy bundle to $Server..."
    Invoke-NativeChecked -FilePath "scp" -Arguments ($scpArgs + @($tempBundle, "${Server}:$remoteBundlePath"))
    Invoke-NativeChecked -FilePath "scp" -Arguments ($scpArgs + @($tempScript, "${Server}:$remoteScriptPath"))

    Write-Host "Deploying to $Server..."
    Invoke-NativeChecked -FilePath "ssh" -Arguments ($sshArgs + @($Server, "bash", $remoteScriptPath))
} finally {
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & git update-ref -d $bundleRef 2>$null
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    if (Test-Path $tempBundle) {
        Remove-Item $tempBundle -Force
    }
    if (Test-Path $tempScript) {
        Remove-Item $tempScript -Force
    }
}

if (!$SkipSmokeTest) {
    Write-Host "Running production smoke test..."
    & "$PSScriptRoot\production-smoke-test.ps1" -BaseUrl $BaseUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Production smoke test failed for $BaseUrl."
    }
}

Write-Host "Production deploy completed for commit $deployCommit."
