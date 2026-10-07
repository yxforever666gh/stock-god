param(
    [ValidateSet('prediction','market','storage','web','contracts')]
    [string[]]$Domain,
    [string[]]$FrontendTest
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $root '.venv\Scripts\python.exe'
$release = Join-Path $PSScriptRoot 'release.py'
$verify = Join-Path $PSScriptRoot 'verify.ps1'
$taskName = 'StockGod-0900-EnsureRunning'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run uv sync --frozen first.' }
$uv = Join-Path $root 'runtime\toolchain\uv\uv.exe'
if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
    $uv = (Get-Command uv.exe -ErrorAction Stop).Source
}

function Invoke-ReleaseJson([string[]]$Arguments) {
    $output = & $python -B $release @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Release command failed: $($Arguments[0])" }
    return ($output | Out-String | ConvertFrom-Json)
}

Push-Location $root
try {
    $plan = Invoke-ReleaseJson @('plan')
    if (-not $plan.major -and -not $Domain.Count) {
        throw 'A small version requires -Domain with the affected domain.'
    }
    if (-not $plan.major -and $plan.frontendChanged -and -not $FrontendTest.Count) {
        throw 'A frontend update requires affected -FrontendTest paths.'
    }
    $null = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $evidence = Join-Path 'H:\Download\stock-god-update' (
        $plan.appVersion + '-' + $plan.commit.Substring(0, 12) + '-' + [guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $evidence -Force | Out-Null
    $log = Join-Path $evidence 'verification.log'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $phase = $clock.Elapsed
    if (($plan.major -or $plan.frontendChanged) -and
        ($plan.frontendLockChanged -or
            -not (Test-Path -LiteralPath (Join-Path $root 'frontend\node_modules') -PathType Container))) {
        $env:npm_config_cache = 'H:\Download\stock-god-build-cache\npm'
        $env:npm_config_proxy = 'http://127.0.0.1:7890'
        $env:npm_config_https_proxy = 'http://127.0.0.1:7890'
        Push-Location (Join-Path $root 'frontend')
        try {
            & npm.cmd ci --no-audit --no-fund *>&1 |
                Tee-Object -FilePath (Join-Path $evidence 'npm-ci.log')
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        } finally {
            Pop-Location
        }
    }
    if ($plan.major) {
        & $verify -Tier major *>&1 | Tee-Object -FilePath $log
        if ($LASTEXITCODE -ne 0) { throw 'Offline major-version verification failed.' }
    } else {
        for ($i = 0; $i -lt $Domain.Count; $i++) {
            $verifyParams = @{ Tier = 'domain'; Domain = $Domain[$i] }
            if ($i -eq 0 -and $FrontendTest.Count) {
                $verifyParams.FrontendTest = $FrontendTest
            }
            & $verify @verifyParams *>&1 | Tee-Object -FilePath $log -Append
            if ($LASTEXITCODE -ne 0) { throw "Domain verification failed: $($Domain[$i])" }
        }
    }
    $verificationSeconds = ($clock.Elapsed - $phase).TotalSeconds
    Write-Output ("verification_seconds={0:n1}" -f $verificationSeconds)
    $phase = $clock.Elapsed
    $builder = @('build','--uv',$uv)
    if ($plan.major) { $builder += '--frontend-ready' }
    if ($plan.frontendChanged) { $builder += '--frontend-deps-ready' }
    & $python -B $release @builder *>&1 | Tee-Object -FilePath (Join-Path $evidence 'build.log')
    if ($LASTEXITCODE -ne 0) { throw 'Snapshot build failed.' }
    $snapshotSeconds = ($clock.Elapsed - $phase).TotalSeconds
    Write-Output ("snapshot_seconds={0:n1}" -f $snapshotSeconds)
    $candidate = Join-Path $root ('runtime\releases\' + $plan.appVersion + '\' + $plan.commit)
    $proofArgs = @('proof','--candidate',$candidate,'--evidence',$log)
    foreach ($name in $Domain) { $proofArgs += @('--domain',$name) }
    $proof = Invoke-ReleaseJson $proofArgs
    $current = Invoke-ReleaseJson @('plan')
    if ($current.commit -ne $plan.commit) { throw 'Checkout changed after verification.' }

    # The scheduler is the production process host. Restart its script only after
    # all checks and the snapshot succeed, so it can perform the sole new start.
    $phase = $clock.Elapsed
    $receiptPath = $null
    try {
        $null = Invoke-ReleaseJson @('stop')
        if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
            Stop-ScheduledTask -TaskName $taskName
        }
        $receipt = Invoke-ReleaseJson @('deploy','--candidate',$candidate,'--proof',$proof.proof)
        $receiptPath = Join-Path $receipt.directory 'receipt.json'
        Start-ScheduledTask -TaskName $taskName
        $deadline = (Get-Date).AddMinutes(3)
        while ((Get-Date) -lt $deadline) {
            $state = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
            if ($state.status -eq 'deployed') { break }
            if ($state.status -in @('rolled_back','aborted')) {
                throw "Deployment $($state.status): $($state.failure)"
            }
            Start-Sleep -Seconds 2
        }
        if ($state.status -ne 'deployed') { throw 'Scheduled activation timed out.' }
        $ready = Invoke-ReleaseJson @('status')
        if ($ready.appVersion -ne $plan.appVersion -or $ready.commit -ne $plan.commit -or
            -not $ready.readiness.ready) {
            throw 'Running service identity or readiness differs from the candidate.'
        }
    } catch {
        $failure = $_
        try {
            if ($receiptPath -and (Test-Path -LiteralPath $receiptPath)) {
                $state = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
                if ($state.status -eq 'deployed') {
                    $null = Invoke-ReleaseJson @('rollback','--receipt',$receiptPath)
                } elseif ($state.status -notin @('rolled_back','aborted')) {
                    $recovered = Invoke-ReleaseJson @('recover')
                    if ($recovered.status -eq 'deployed') {
                        $null = Invoke-ReleaseJson @('rollback','--receipt',$receiptPath)
                    }
                }
            } elseif (Test-Path -LiteralPath (Join-Path $root 'runtime\deployments\pending.json')) {
                $recovered = Invoke-ReleaseJson @('recover')
                if ($recovered.status -eq 'deployed') {
                    $path = Join-Path $recovered.directory 'receipt.json'
                    $null = Invoke-ReleaseJson @('rollback','--receipt',$path)
                }
            }
            $null = Invoke-ReleaseJson @('stop')
            if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
                Stop-ScheduledTask -TaskName $taskName
            }
            Start-ScheduledTask -TaskName $taskName
        } catch {
            Write-Warning "Automatic recovery needs attention: $($_.Exception.Message)"
        }
        throw $failure
    }
    $deploymentSeconds = ($clock.Elapsed - $phase).TotalSeconds
    Write-Output ("deployment_seconds={0:n1}" -f $deploymentSeconds)
    if ((git rev-parse HEAD) -ne $plan.commit) {
        throw 'Checkout changed after deployment; tag was not created.'
    }
    git tag -a $plan.appVersion $plan.commit -m "Stock God $($plan.appVersion)"
    if ($LASTEXITCODE -ne 0) { throw 'Deployment succeeded but local tag creation failed.' }

    if ($plan.major) {
        $remote = git remote get-url origin
        if ($remote -notmatch '^git@github\.com:') { throw 'GitHub remote must use SSH.' }
        $ssh = ssh -G github.com
        if ($LASTEXITCODE -ne 0 -or -not ($ssh -match '(?m)^hostname ssh\.github\.com$') -or
            -not ($ssh -match '(?m)^port 443$') -or -not ($ssh -match '(?m)^proxycommand ')) {
            throw 'GitHub SSH proxy configuration is unavailable; direct fallback is prohibited.'
        }
        $priorShell = $env:SHELL
        try {
            $env:SHELL = 'H:/Program Files (x86)/Git/bin/bash.exe'
            git push --atomic origin "$($plan.commit):refs/heads/main" "refs/tags/$($plan.appVersion)"
            if ($LASTEXITCODE -ne 0) { throw 'GitHub push failed; local deployment and tag remain.' }
            $remoteRefs = git ls-remote origin 'refs/heads/main' "refs/tags/$($plan.appVersion)^{}"
            if ($LASTEXITCODE -ne 0) { throw 'Could not verify remote SHA.' }
            $hashes = @($remoteRefs | ForEach-Object { ($_ -split '\s+')[0] })
            if ($hashes.Count -ne 2 -or @($hashes | Where-Object { $_ -ne $plan.commit }).Count) {
                throw 'Remote branch or tag SHA differs from the deployed commit.'
            }
        } finally {
            $env:SHELL = $priorShell
        }
    }
    [ordered]@{
        version = $plan.appVersion
        commit = $plan.commit
        verificationSeconds = [Math]::Round($verificationSeconds, 1)
        snapshotSeconds = [Math]::Round($snapshotSeconds, 1)
        deploymentSeconds = [Math]::Round($deploymentSeconds, 1)
        totalSeconds = [Math]::Round($clock.Elapsed.TotalSeconds, 1)
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $evidence 'timing.json') -Encoding utf8
    Write-Output ("total_seconds={0:n1} version={1} commit={2}" -f
        $clock.Elapsed.TotalSeconds,$plan.appVersion,$plan.commit)
} finally {
    Pop-Location
}
