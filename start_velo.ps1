# start_velo.ps1 - run this every time you start working on Velo Claim.

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$SshUser = "dev1"
$SshHost = "2.51.50.72"
$SshPort = 2222

$PostgresDsn = "postgresql://velo_claim:velo_claim_dev_password@localhost:5433/velo_claim"
$RedisHost = "localhost"
$RedisPort = 6379
$MinioEndpoint = "http://localhost:9000"
$MinioAccessKey = "velo_claim"
$MinioSecretKey = "velo_claim_dev_password"

Write-Host "Starting Velo Claim dev environment..." -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor DarkGray

Write-Host "Checking DGX containers..." -ForegroundColor Yellow
ssh -p $SshPort "$SshUser@$SshHost" "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep velo-claim || true"

Write-Host "Closing old SSH tunnels..." -ForegroundColor Yellow
Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq "ssh.exe" -and
        $_.CommandLine -match "-L 5433:" -and
        $_.CommandLine -match [regex]::Escape($SshHost)
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  stopped tunnel process $($_.ProcessId)" -ForegroundColor DarkGray
    }

Write-Host "Opening SSH tunnel..." -ForegroundColor Yellow
$TunnelCommand = "ssh -p $SshPort -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 5433:127.0.0.1:5433 -L 6379:127.0.0.1:6379 -L 9000:127.0.0.1:9000 -L 9001:127.0.0.1:9001 $SshUser@$SshHost -N"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $TunnelCommand

Start-Sleep -Seconds 3

Write-Host "Testing connections..." -ForegroundColor Yellow
$env:VELO_START_POSTGRES_DSN = $PostgresDsn
$env:VELO_START_REDIS_HOST = $RedisHost
$env:VELO_START_REDIS_PORT = [string]$RedisPort
$env:VELO_START_MINIO_ENDPOINT = $MinioEndpoint
$env:VELO_START_MINIO_ACCESS_KEY = $MinioAccessKey
$env:VELO_START_MINIO_SECRET_KEY = $MinioSecretKey

@'
import os

import boto3
import psycopg
import redis
from botocore.config import Config

postgres_dsn = os.environ["VELO_START_POSTGRES_DSN"]
redis_host = os.environ["VELO_START_REDIS_HOST"]
redis_port = int(os.environ["VELO_START_REDIS_PORT"])
minio_endpoint = os.environ["VELO_START_MINIO_ENDPOINT"]
minio_access_key = os.environ["VELO_START_MINIO_ACCESS_KEY"]
minio_secret_key = os.environ["VELO_START_MINIO_SECRET_KEY"]

try:
    conn = psycopg.connect(postgres_dsn, connect_timeout=10)
    conn.close()
    print("  Postgres  OK")
except Exception as e:
    print(f"  Postgres  FAIL: {e}")

try:
    r = redis.Redis(host=redis_host, port=redis_port, db=0, socket_connect_timeout=10, socket_timeout=10)
    r.ping()
    print("  Redis     OK")
except Exception as e:
    print(f"  Redis     FAIL: {e}")

try:
    s3 = boto3.client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id=minio_access_key,
        aws_secret_access_key=minio_secret_key,
        config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 0}),
    )
    buckets = [bucket["Name"] for bucket in s3.list_buckets()["Buckets"]]
    print(f"  MinIO     OK: {buckets}")
except Exception as e:
    print(f"  MinIO     FAIL: {e}")

print("")
print("All systems ready. Happy coding!")
'@ | python -

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Keep the SSH tunnel PowerShell window open."
Write-Host "  2. Start the backend: npm run api"
Write-Host "  3. Start the frontend: npm run dev"
Write-Host "  4. Open frontend: http://127.0.0.1:5174"
Write-Host "  5. API health: http://127.0.0.1:8002/health"
