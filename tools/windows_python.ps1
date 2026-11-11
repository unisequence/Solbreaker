# Prefer the private runtime in self-contained release packages.
function Get-SolbreakerBundledPython {
    $directory = Join-Path $PSScriptRoot '..\runtime\python'
    if (-not (Test-Path -LiteralPath $directory)) { return $null }
    $python = Join-Path $directory 'python.exe'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath (Join-Path $directory 'solbreaker-bundle.json'))) {
        throw 'The bundled Python runtime is incomplete. Extract the complete Solbreaker archive again.'
    }
    return (Resolve-Path -LiteralPath $python).Path
}

# Discover CPython without requiring the optional launcher or a PATH entry.
function Get-SolbreakerRegisteredPython {
    foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
            $base = $null
            $companies = $null
            try {
                $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
                $companies = $base.OpenSubKey('Software\Python')
                if (-not $companies) { continue }
                foreach ($companyName in $companies.GetSubKeyNames()) {
                    $company = $companies.OpenSubKey($companyName)
                    if (-not $company) { continue }
                    try {
                        foreach ($tag in ($company.GetSubKeyNames() | Sort-Object -Descending)) {
                            $install = $company.OpenSubKey($tag + '\InstallPath')
                            if (-not $install) { continue }
                            try {
                                $executable = $install.GetValue('ExecutablePath')
                                if (-not $executable) {
                                    $directory = $install.GetValue('')
                                    if ($directory) { $executable = Join-Path $directory 'python.exe' }
                                }
                                if ($executable -is [string] -and $executable) { $executable }
                            } finally { $install.Dispose() }
                        }
                    } finally { $company.Dispose() }
                }
            } catch [System.Security.SecurityException] {
                # A restricted hive does not prevent using another installation.
            } catch [System.UnauthorizedAccessException] {
            } finally {
                if ($companies) { $companies.Dispose() }
                if ($base) { $base.Dispose() }
            }
        }
    }
}

function Find-SolbreakerPython {
    param([switch]$Bundled)
    $candidates = [System.Collections.Generic.List[object]]::new()
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($launcher) {
        $selector = if ($Bundled) { '-3.12' } else { '-3' }
        $candidates.Add(@{Executable = $launcher.Source; Arguments = @($selector)})
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $application = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($application -and $application.Source -notmatch '\\Microsoft\\WindowsApps\\') {
            $candidates.Add(@{Executable = $application.Source; Arguments = @()})
        }
    }
    foreach ($executable in (Get-SolbreakerRegisteredPython | Select-Object -Unique)) {
        $candidates.Add(@{Executable = $executable; Arguments = @()})
    }
    $versionCheck = if ($Bundled) { 'sys.version_info[:2] == (3, 12)' } else { 'sys.version_info >= (3, 11)' }
    $probe = @"
import json, platform, struct, sys
valid = sys.implementation.name == 'cpython' and $versionCheck and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64')
if not valid:
    raise SystemExit(1)
print(json.dumps(sys.executable))
"@
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Executable -PathType Leaf)) { continue }
        $arguments = @($candidate.Arguments) + @('-I', '-S', '-c', $probe)
        $previousPreference = $ErrorActionPreference
        try {
            # Windows PowerShell treats stderr from unsuccessful native probes as errors.
            $ErrorActionPreference = 'Continue'
            $output = & $candidate.Executable @arguments 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $resolved = ($output -join "`n") | ConvertFrom-Json -ErrorAction Stop
            if ($resolved -is [string] -and (Test-Path -LiteralPath $resolved -PathType Leaf)) {
                return $resolved
            }
        } catch {
            # A stale registration or unusable PATH entry must not hide a valid interpreter.
        } finally { $ErrorActionPreference = $previousPreference }
    }
    return $null
}
