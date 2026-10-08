param(
    [ValidateSet('fast','domain','major','release')][string]$Tier = 'fast',
    [ValidateSet('prediction','market','storage','web','contracts')][string]$Domain,
    [string[]]$TestPath,
    [string[]]$FrontendTest
)
$ErrorActionPreference = 'Stop'
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonCommand = Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonCommand -PathType Leaf)) { throw 'Run uv sync --frozen first.' }
$env:PYTHONUTF8 = '1'
$temporaryDirectory = Join-Path 'H:\Download\stock-god-validation' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryDirectory -Force | Out-Null
function Invoke-Check([string]$Program, [string[]]$CommandArguments) {
    & $Program @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw "Verification failed: $Program (exit $LASTEXITCODE)" }
}
Push-Location $projectDirectory
try {
    $paths = @($TestPath | Where-Object { $_ })
    if ($Domain) {
        $paths = switch ($Domain) {
            'prediction' { @('tests/prediction','tests/ai','tests/test_audit.py','tests/test_evidence_store.py','tests/test_settings.py') }
            'market' { @('tests/market') }
            'storage' { @('tests/storage') }
            'web' { @('tests/test_app.py') }
            'contracts' { @('tests/test_contracts.py','tests/test_boundaries.py') }
        }
    }
    if ($Tier -in @('major','release')) { $paths = @('tests') }
    if (-not $paths.Count -and -not $FrontendTest.Count) { throw 'Choose -TestPath, -FrontendTest, or -Domain for local verification.' }
    if ($paths.Count) {
        foreach ($path in $paths) {
            $resolved = [IO.Path]::GetFullPath((Join-Path $projectDirectory $path))
            if (-not $resolved.StartsWith($projectDirectory + [IO.Path]::DirectorySeparatorChar) -or -not (Test-Path -LiteralPath $resolved)) {
                throw "Unknown project test target: $path"
            }
        }
        $marker = if ($Tier -in @('major','release') -or $Domain -eq 'storage') { 'not live and not browser' } else { 'not migration and not live and not browser' }
        Invoke-Check $pythonCommand (@('-m','pytest','-q','--tb=short','-m',$marker,'--basetemp',(Join-Path $temporaryDirectory 'pytest')) + $paths)
    }
    if ($Tier -eq 'release') {
        Invoke-Check $pythonCommand @('-m','ruff','check','src/stock_god','scripts/release.py')
        Invoke-Check $pythonCommand @('-m','pyright')
    }
    if ($Tier -in @('major','release')) {
        Invoke-Check $pythonCommand @('-m','stock_god.contracts')
    }
    if ($Tier -in @('major','release') -or $FrontendTest.Count) {
        Push-Location (Join-Path $projectDirectory 'frontend')
        try {
            if ($Tier -eq 'release') {
                Invoke-Check 'npm.cmd' @('run','lint')
                Invoke-Check 'npm.cmd' @('run','test:runtime')
                Invoke-Check 'npm.cmd' @('run','build')
            } elseif ($Tier -eq 'major') {
                Invoke-Check 'npm.cmd' @('run','test:runtime')
                Invoke-Check 'npm.cmd' @('run','build')
            } else { Invoke-Check 'node.exe' (@('--test') + $FrontendTest) }
        } finally { Pop-Location }
    }
    Invoke-Check 'git' @('diff','--check')
    Write-Output "Verification passed: $Tier; temporary evidence: $temporaryDirectory"
} finally { Pop-Location }
