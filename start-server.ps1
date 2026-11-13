[CmdletBinding()]
param([string]$Distribution = 'Ubuntu', [string]$State, [switch]$Owned, [switch]$UseWsl, [switch]$Sandbox)
$ErrorActionPreference = 'Stop'
if (-not $State) { $State = Join-Path $PSScriptRoot 'data' }
if (-not $UseWsl) {
    Set-Location -LiteralPath $PSScriptRoot
    . (Join-Path $PSScriptRoot 'tools\windows_python.ps1')
    $python = Get-SolbreakerBundledPython
    if (-not $python) { $python = Join-Path $PSScriptRoot '.venv-gui\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Prepare the native Windows environment with setup.ps1 first.'
    }
    $defaults = @{
        SOLBREAKER_SIM_BACKEND='rust'; SOLBREAKER_DYNAMIC_BACKEND='rapier'; SOLBREAKER_ARENA_OWNERS='1';
        SOLBREAKER_OWNER_IPC='socket'; SOLBREAKER_SIM_WORKERS='0'; SOLBREAKER_RAPIER_PARALLEL_DRIVE='1';
        SOLBREAKER_RAPIER_QUERIES='bullet'; SOLBREAKER_DRIVE_PREFIX='1'; SOLBREAKER_ROTATION_PREFIX='1';
        SOLBREAKER_SUPPORT_PIPELINE='1'; SOLBREAKER_REPLICA_PIPELINE='1'; SOLBREAKER_MARKER_BATCH='1';
        SOLBREAKER_GROUND_RAY_BATCH='1'; SOLBREAKER_MAX_ARENAS='1'; OPENBLAS_NUM_THREADS='1'; OMP_NUM_THREADS='1'
    }
    foreach ($name in $defaults.Keys) {
        if (-not (Test-Path "Env:$name")) { Set-Item "Env:$name" $defaults[$name] }
    }
    $arguments = @('-u', '-m', 'solbreaker', 'serve', '--state', $State,
                   '--log-file', (Join-Path $State 'server.log'))
    if ($Owned) { $arguments += '--stop-on-stdin' }
    if ($Sandbox) { $arguments += '--sandbox' }
    & $python @arguments
    exit $LASTEXITCODE
}
$linuxProject = & wsl.exe --distribution $Distribution --exec wslpath -a -u $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Cannot find project inside WSL.' }
$linuxProject = $linuxProject.Trim()
$statePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($State)
$linuxState = & wsl.exe --distribution $Distribution --exec wslpath -a -u $statePath
if ($LASTEXITCODE -ne 0) { throw 'Cannot map the server state into WSL.' }
$linuxState = $linuxState.Trim()
$hostAddress = & wsl.exe --distribution $Distribution --exec python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('192.0.2.1',9)); print(s.getsockname()[0]); s.close()"
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine the WSL server address.' }
& wsl.exe --distribution $Distribution --exec "$linuxProject/.venv/bin/python" "$linuxProject/tools/refresh_wsl_network.py" --state $linuxState --host ($hostAddress.Trim())
if ($LASTEXITCODE -ne 0) { throw 'Could not update the WSL connection profile.' }
$arguments = @('bash', "$linuxProject/start-server.sh", '--state', $linuxState, '--log-file', "$linuxState/server.log")
if ($Owned) { $arguments += '--stop-on-stdin' }
if ($Sandbox) { $arguments += '--sandbox' }
& wsl.exe --distribution $Distribution --exec @arguments
exit $LASTEXITCODE
