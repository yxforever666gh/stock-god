param(
    [ValidateSet('build','inspect','deploy','rollback','recover','start','stop','restart','status','ensure')]
    [string]$Command = 'status',
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonCommand = Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonCommand)) {
    $pointerPath = Join-Path $projectDirectory 'runtime\current.json'
    if (Test-Path -LiteralPath $pointerPath) {
        $pointer = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
        if ($pointer.kind -eq 'python') { $pythonCommand = $pointer.pythonExecutable }
    }
}
if (-not (Test-Path -LiteralPath $pythonCommand -PathType Leaf)) { throw 'Run uv sync --frozen with the project Python first.' }
$env:PYTHONUTF8 = '1'
& $pythonCommand -B (Join-Path $PSScriptRoot 'release.py') $Command @Arguments
if ($LASTEXITCODE -ne 0) { throw "Release operation failed (exit $LASTEXITCODE)" }
