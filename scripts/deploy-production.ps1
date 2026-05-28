param(
    [string]$Commit = "",
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$Server = "kmh251@10.4.51.232",
    [string]$SshKey = "",
    [string]$RemotePath = "/home/kmh251/deployment/portal-search-agent",
    [string]$BaseUrl = "https://knowledgebase-search.251gh.local",
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

$quotedRemotePath = ConvertTo-ShellLiteral -Value $RemotePath
$quotedDeployCommit = ConvertTo-ShellLiteral -Value $deployCommit
$quotedBranch = ConvertTo-ShellLiteral -Value $Branch
$quotedRemoteBranch = ConvertTo-ShellLiteral -Value "origin/$Branch"

$remoteScript = @"
set -eu
cd $quotedRemotePath

echo "Checking production checkout..."
git fetch origin $quotedBranch

if [ -n "`$(git status --porcelain=v1)" ]; then
    echo "Production deploy blocked because the production checkout is not clean:" >&2
    git status --short >&2
    exit 20
fi

git merge-base --is-ancestor $quotedDeployCommit $quotedRemoteBranch
git checkout --detach $quotedDeployCommit

docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml up -d --build opensearch tika app
docker compose -f docker-compose.prod.yml ps
"@

if ($DryRun) {
    Write-Host "Dry run only. Remote script that would be executed:"
    Write-Host $remoteScript
    exit 0
}

$sshArgs = @()
if (![string]::IsNullOrWhiteSpace($SshKey)) {
    $sshArgs += @("-i", $SshKey)
}
$sshArgs += @("-o", "StrictHostKeyChecking=no", $Server, "bash -s")

Write-Host "Deploying to $Server..."
$remoteScript | & ssh @sshArgs
if ($LASTEXITCODE -ne 0) {
    throw "Production deploy failed on $Server."
}

if (!$SkipSmokeTest) {
    Write-Host "Running production smoke test..."
    & "$PSScriptRoot\production-smoke-test.ps1" -BaseUrl $BaseUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Production smoke test failed for $BaseUrl."
    }
}

Write-Host "Production deploy completed for commit $deployCommit."
