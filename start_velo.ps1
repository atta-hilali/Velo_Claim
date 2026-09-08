# start_velo.ps1 - one-click launcher for the DGX-backed Velo Claim environment.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$SshUser = "dev1"
$SshHost = "2.51.71.201"
$SshPort = 2222
$RemoteContainer = "velo-claim-api"
$ApiUrl = "http://127.0.0.1:8000"
$FrontendUrl = "http://127.0.0.1:5173"

function Invoke-DgxCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RemoteCommand,
        [int]$TimeoutSeconds = 15
    )

    $Job = Start-Job -ScriptBlock {
        param($RemoteUser, $RemoteHost, $RemotePort, $Command)
        $Output = & ssh.exe `
            -p $RemotePort `
            -o BatchMode=yes `
            -o ConnectTimeout=10 `
            -o ConnectionAttempts=1 `
            "$RemoteUser@$RemoteHost" `
            $Command 2>&1
        [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output = ($Output -join [Environment]::NewLine).Trim()
        }
    } -ArgumentList $SshUser, $SshHost, $SshPort, $RemoteCommand
    try {
        if (-not (Wait-Job -Job $Job -Timeout $TimeoutSeconds)) {
            Stop-Job -Job $Job -ErrorAction SilentlyContinue
            throw "SSH command timed out after $TimeoutSeconds seconds."
        }
        $Result = Receive-Job -Job $Job
        if ($Result.ExitCode -ne 0) {
            $Details = if ($Result.Output) { $Result.Output } else { "No SSH error output." }
            throw "SSH command failed with exit code $($Result.ExitCode): $Details"
        }
        return $Result.Output
    }
    finally {
        Remove-Job -Job $Job -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Starting Velo Claim..." -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor DarkGray

Write-Host "Checking the DGX backend container..." -ForegroundColor Yellow
try {
    $SshProbe = Invoke-DgxCommand -RemoteCommand "printf reachable"
}
catch {
    throw "Cannot reach the DGX over SSH at ${SshHost}:${SshPort}. Check the VPN/network, public IP, firewall, SSH key, and SSH port. Details: $($_.Exception.Message)"
}
if ($SshProbe -ne "reachable") {
    throw "DGX SSH probe returned an unexpected response: $SshProbe"
}
Write-Host "  DGX SSH reachable" -ForegroundColor Green

try {
    $RemoteStatus = Invoke-DgxCommand -RemoteCommand "docker inspect -f '{{.State.Status}}' $RemoteContainer 2>/dev/null"
}
catch {
    throw "DGX is reachable, but container $RemoteContainer could not be inspected. Run deploy/docker/deploy_api.sh on DGX. Details: $($_.Exception.Message)"
}
Write-Host "  Backend container status: $RemoteStatus" -ForegroundColor DarkGray

if ($RemoteStatus -ne "running") {
    Write-Host "Starting the DGX backend container..." -ForegroundColor Yellow
    try {
        Invoke-DgxCommand -RemoteCommand "docker start $RemoteContainer" | Out-Null
    }
    catch {
        throw "DGX container $RemoteContainer could not be started."
    }
}
Write-Host "  Backend container running" -ForegroundColor Green

Write-Host "Replacing any old API/database tunnel..." -ForegroundColor Yellow
Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq "ssh.exe" -and
        $_.CommandLine -match "-L 8000:(127\.0\.0\.1|localhost):8000"
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

$TunnelArguments = @(
    "-p", $SshPort,
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-L", "8000:127.0.0.1:8000",
    "-L", "5433:127.0.0.1:5433",
    "$SshUser@$SshHost",
    "-N"
)

$TunnelProcess = Start-Process `
    -FilePath "ssh.exe" `
    -ArgumentList $TunnelArguments `
    -WindowStyle Hidden `
    -PassThru

Write-Host "Waiting for API health..." -ForegroundColor Yellow
$Health = $null
for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
    if ($TunnelProcess.HasExited) {
        throw "The SSH API tunnel stopped unexpectedly."
    }

    try {
        $Health = Invoke-RestMethod -Uri "$ApiUrl/health" -TimeoutSec 3
        break
    }
    catch {
        Start-Sleep -Seconds 1
    }
}

if ($null -eq $Health -or $Health.status -ne "ok") {
    Stop-Process -Id $TunnelProcess.Id -Force -ErrorAction SilentlyContinue
    throw "The backend did not become healthy through the SSH tunnel."
}

Write-Host "  API healthy: $($Health.storage), $($Health.object_store), $($Health.cache)" -ForegroundColor Green

$FrontendRunning = $false
try {
    Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
    $FrontendRunning = $true
}
catch {
    $FrontendRunning = $false
}

if (-not $FrontendRunning) {
    Write-Host "Starting the frontend..." -ForegroundColor Yellow
    $EscapedProjectRoot = $ProjectRoot.Replace("'", "''")
    $FrontendCommand = "Set-Location -LiteralPath '$EscapedProjectRoot'; npm run dev"
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList "-NoExit", "-Command", $FrontendCommand

    for ($Attempt = 1; $Attempt -le 30; $Attempt++) {
        try {
            Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
            $FrontendRunning = $true
            break
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
}

if (-not $FrontendRunning) {
    throw "The frontend did not start at $FrontendUrl."
}

Write-Host "  Frontend ready" -ForegroundColor Green
Start-Process $FrontendUrl

Write-Host ""
Write-Host "Velo Claim is ready." -ForegroundColor Cyan
Write-Host "  Frontend: $FrontendUrl"
Write-Host "  API:      $ApiUrl"
Write-Host "  Tunnel PID: $($TunnelProcess.Id)" -ForegroundColor DarkGray
