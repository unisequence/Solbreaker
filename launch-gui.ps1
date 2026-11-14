[CmdletBinding()]
param([string]$Config)
$ErrorActionPreference = 'Stop'
trap {
    $directory = Join-Path $PSScriptRoot 'runtime'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $_ | Out-String | Out-File -FilePath (Join-Path $directory 'launcher-bootstrap.log') -Append -Encoding utf8
    exit 1
}
if (-not $Config) { $Config = Join-Path $PSScriptRoot 'runtime\manager.json' }
Set-Location -LiteralPath $PSScriptRoot
$stage = Join-Path $PSScriptRoot '.solbreaker-update'
if (Test-Path -LiteralPath (Join-Path $stage 'transaction')) {
    throw 'Solbreaker update was interrupted. See runtime/launcher-update.log.'
}
$wheel = Join-Path $PSScriptRoot 'wheels\pybullet-3.2.7-cp312-cp312-win_amd64.whl'
$bundled = Test-Path -LiteralPath $wheel
$requirement = if ($bundled) { 'Python 3.12 x64' } else { 'Python 3.11 or newer' }
. (Join-Path $PSScriptRoot 'tools\windows_python.ps1')
$python = Get-SolbreakerBundledPython
$portable = [bool]$python
if (-not $python) { $python = Join-Path $PSScriptRoot '.venv-gui\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $python)) {
    . (Join-Path $PSScriptRoot 'tools\windows_python.ps1')
    $basePython = Find-SolbreakerPython -Bundled:$bundled
    if (-not $basePython) {
        throw "Install $requirement from python.org, then open launch-gui.cmd again."
    }
    & $basePython -m venv .venv-gui
    if ($LASTEXITCODE -ne 0) { throw "$requirement is required." }
}
if ($bundled) {
    & $python -c "import sys,struct,platform; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64') else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'The native Windows package requires Python 3.12 x64. Use a matching Python environment.' }
}
$previousPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $python tools/install_dependencies.py --profile gui --check 2>$null
    $dependenciesReady = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = $previousPreference
}
if ($portable -and -not $dependenciesReady) { throw 'Bundled dependencies are incomplete. Extract the complete Solbreaker archive again.' }
if (-not $dependenciesReady) {
    & $python tools/install_dependencies.py --profile gui
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the launcher dependencies.' }
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is missing from the Python environment.' }
$entry = Join-Path $PSScriptRoot 'tools\launch_gui.pyw'
$arguments = '"' + $entry + '" "' + $Config + '"'
$env:SOLBREAKER_RELEASE_LAUNCHER = ''
$gui = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -Wait -PassThru
if ($gui.ExitCode -ne 42) { exit $gui.ExitCode }

$updatePython = Join-Path $stage 'payload\runtime\python\python.exe'
$updateScript = Join-Path $stage 'apply.py'
$log = Join-Path $PSScriptRoot 'runtime\launcher-update.log'
$previousPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $LASTEXITCODE = 1
    & $updatePython -B $updateScript *> $log
    $updated = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = $previousPreference
}
if ($updated) {
    $env:SOLBREAKER_UPDATE_RESULT = 'success'
} elseif (Test-Path -LiteralPath (Join-Path $stage 'transaction')) {
    throw "Solbreaker update could not restore the previous release. See $log."
} else {
    $env:SOLBREAKER_UPDATE_RESULT = 'failed'
}
Remove-Item -LiteralPath $stage -Recurse -Force
& (Join-Path $PSScriptRoot 'launch-gui.ps1') -Config $Config
exit $LASTEXITCODE
