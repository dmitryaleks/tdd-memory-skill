# Thin launcher: the real installer is install.py, so the logic lives in one
# tested place instead of being duplicated across two shells.
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'install.py'
foreach ($name in @('python', 'python3', 'py')) {
    $exe = Get-Command $name -ErrorAction SilentlyContinue
    if ($exe) { & $exe.Source $script @args; exit $LASTEXITCODE }
}
Write-Error 'install: need python 3.8+ on PATH'
exit 1
