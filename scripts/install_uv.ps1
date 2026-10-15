param(
    [int]$TimeoutSeconds = 120,
    [string]$InstallerCommand = 'irm https://astral.sh/uv/install.ps1 | iex'
)

$ErrorActionPreference = 'Stop'
try {
    Write-Host "Installing uv (timeout: $TimeoutSeconds seconds)..."
    # A separate process bounds both downloading the script and its installation work.
    $command = '$ErrorActionPreference = ''Stop''; ' + $InstallerCommand
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process -FilePath "$PSHOME\powershell.exe" -NoNewWindow -PassThru `
        -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'ByPass', '-EncodedCommand', $encoded)
    # Retain the handle so Windows PowerShell can read ExitCode after waiting.
    $processHandle = $process.Handle
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
        Write-Host "ERROR: uv installation timed out after $TimeoutSeconds seconds."
        Write-Host 'Enable your VPN/proxy, then run start_web.bat again to download uv.'
        exit 1
    }
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "Installer exited with code $($process.ExitCode)."
    }
    Write-Host 'uv installer completed.'
    exit 0
} catch {
    Write-Host "ERROR: uv installation failed: $_"
    Write-Host 'Enable your VPN/proxy, then run start_web.bat again to download uv.'
    exit 1
}
