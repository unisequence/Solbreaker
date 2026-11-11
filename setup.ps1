[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Game,
    [string]$Client,
    [string]$State,
    [switch]$SkipBuild,
    [string]$Distribution = 'Ubuntu',
    [int]$HttpPort = 18080,
    [int]$LoginPort = 20016,
    [string]$BindAddress = '127.0.0.1',
    [switch]$Resume,
    [switch]$UseWsl
)
$ErrorActionPreference = 'Stop'
if (-not $State) { $State = Join-Path $PSScriptRoot 'data' }

if (-not $UseWsl) {
    Set-Location -LiteralPath $PSScriptRoot
    $wheel = Join-Path $PSScriptRoot 'wheels\pybullet-3.2.7-cp312-cp312-win_amd64.whl'
    $bundled = Test-Path -LiteralPath $wheel
    . (Join-Path $PSScriptRoot 'tools\windows_python.ps1')
    $python = Get-SolbreakerBundledPython
    if ($python) { $SkipBuild = $true }
    else { $python = Join-Path $PSScriptRoot '.venv-gui\Scripts\python.exe' }
    $arguments = @('tools/setup.py', '--game', $Game, '--state', $State,
                   '--host', $BindAddress, '--http-port', "$HttpPort", '--login-port', "$LoginPort")
    if ($Client) { $arguments += @('--client', $Client, '--windows-client') }
    if ($Resume) { $arguments += '--resume' }
    if ($SkipBuild) { $arguments += '--skip-build' }
    if (Test-Path -LiteralPath $python) {
        & $python @arguments
    } else {
        . (Join-Path $PSScriptRoot 'tools\windows_python.ps1')
        $basePython = Find-SolbreakerPython -Bundled:$bundled
        if (-not $basePython) { throw 'Install a compatible Python from python.org before preparing Solbreaker.' }
        & $basePython @arguments
    }
    if ($LASTEXITCODE -ne 0) { throw 'Solbreaker setup failed. See the preceding error.' }
    exit 0
}

function Invoke-Wsl {
    param([string[]]$Command)
    $output = & wsl.exe --distribution $Distribution --exec @Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed. Install WSL2 and the selected Linux distribution first."
    }
    return $output
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'WSL2 is required. Run wsl --install -d Ubuntu, restart Windows if requested, then retry.'
}
$project = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$gamePath = (Resolve-Path -LiteralPath $Game).Path
$linuxProject = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $project)).Trim()
$linuxGame = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $gamePath)).Trim()
$statePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($State)
$linuxState = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $statePath)).Trim()
# NAT-mode UDP uses the WSL address directly. TCP localhost forwarding alone
# cannot provide the game's LoginApp and BaseApp UDP connection.
$hostAddress = (Invoke-Wsl -Command @('python3', '-c', "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('192.0.2.1',9)); print(s.getsockname()[0]); s.close()")).Trim()
$arguments = @('bash', "$linuxProject/setup.sh", '--game', $linuxGame,
               '--state', $linuxState, '--host', $hostAddress, '--http-port', "$HttpPort", '--login-port', "$LoginPort")
if ($Client) {
    $clientPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Client)
    if (($clientPath + '\client\wotblitz.exe').Length -ge 260) {
        throw 'Choose a shorter client path. The pinned game uses a 260-character module-path buffer.'
    }
    $linuxClient = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $clientPath)).Trim()
    $arguments += @('--client', $linuxClient, '--windows-client')
}
if ($Resume) { $arguments += '--resume' }
if ($SkipBuild) { $arguments += '--skip-build' }
& wsl.exe --distribution $Distribution --exec @arguments
if ($LASTEXITCODE -ne 0) { throw 'Solbreaker setup failed. See the preceding error.' }
Write-Host "Start the server with .\start-server.ps1 -Distribution $Distribution"
if ($Client) { Write-Host "Start the game using $clientPath\start-client.cmd" }
Write-Host 'start-server.ps1 refreshes the WSL address and the prepared client profile on each start.'
