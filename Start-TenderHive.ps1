param(
    [string]$EnvFile,
    [ValidateRange(1, 65535)]
    [int]$Port = 8081,
    [ValidatePattern('^[a-z0-9][a-z0-9_-]*$')]
    [string]$ProjectName = 'tenderhive-dev',
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$tenderRuntimeDirectory = Join-Path $env:LOCALAPPDATA 'TenderHive/dashboard'
if ($ProjectName -ne 'tenderhive-dashboard') {
    $tenderRuntimeDirectory = Join-Path $tenderRuntimeDirectory $ProjectName
}
$tenderRuntimeEnv = Join-Path $tenderRuntimeDirectory 'local.env'

function New-LocalSecret([int]$ByteCount) {
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] $ByteCount
        $generator.GetBytes($bytes)
        return [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $generator.Dispose()
    }
}

function Assert-DockerResult([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

Push-Location -LiteralPath $PSScriptRoot
$previousDashboardPort = [Environment]::GetEnvironmentVariable('TH_DASHBOARD_PORT', 'Process')
try {
    $env:TH_DASHBOARD_PORT = [string]$Port
    if (!(Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install and start Docker Desktop.' }
    docker info --format '{{.ServerVersion}}' | Out-Null
    Assert-DockerResult 'Docker Desktop must be running.'

    if (!$EnvFile) {
        $EnvFile = Join-Path $PSScriptRoot '.env'
        if (!(Test-Path -LiteralPath $EnvFile -PathType Leaf) -and (Get-Command git -ErrorAction SilentlyContinue)) {
            $commonDirectory = git rev-parse --path-format=absolute --git-common-dir 2>$null
            if ($LASTEXITCODE -eq 0 -and $commonDirectory) {
                $EnvFile = Join-Path (Split-Path -Parent $commonDirectory) '.env'
            }
        }
    }
    if (!(Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw 'Set up .env with OpenRouter settings or pass -EnvFile <path>.'
    }
    $EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path

    if (!(Test-Path -LiteralPath $tenderRuntimeEnv -PathType Leaf)) {
        [IO.Directory]::CreateDirectory($tenderRuntimeDirectory) | Out-Null
        $settings = @(
            "DASH_JWT_SECRET=$(New-LocalSecret 32)"
            "DASH_SERVICE_KEY=$(New-LocalSecret 32)"
            "DASH_WORKBENCH_TOKEN=$(New-LocalSecret 32)"
            "DASH_DEMO_PASSWORD=$(New-LocalSecret 12)"
        )
        [IO.File]::WriteAllLines($tenderRuntimeEnv, $settings, [Text.UTF8Encoding]::new($false))
    }
    $compose = @(
        '-p', $ProjectName,
        '--env-file', '.env.example', '--env-file', $EnvFile, '--env-file', $tenderRuntimeEnv,
        '-f', 'compose.yaml', '-f', 'infra/demo/compose.yaml',
        '-f', 'infra/demo/web.compose.yaml', '-f', 'infra/demo/dashboard.compose.yaml',
        '--profile', 'dashboard', '--profile', 'web', '--profile', 'orchestrator',
        '--profile', 'solver', '--profile', 'agents', '--profile', 'eval',
        '--profile', 'modelgw', '--profile', 'kb', '--profile', 'demo'
        '--profile', 'docintel'
    )
    if ($Stop) {
        docker compose @compose stop
        Assert-DockerResult 'Could not stop the dashboard project.'
        return
    }

    $resolved = docker compose @compose config --environment
    Assert-DockerResult 'Compose configuration is invalid.'
    $envValues = @{}
    foreach ($line in $resolved) {
        $separator = $line.IndexOf('=')
        if ($separator -gt 0) { $envValues[$line.Substring(0, $separator)] = $line.Substring($separator + 1) }
    }
    if ($envValues['MODEL_GW_PROVIDER'] -ne 'openrouter') { throw 'Set MODEL_GW_PROVIDER=openrouter in .env.' }
    foreach ($name in @('OPENROUTER_API_KEY', 'OPENROUTER_MODEL_TEST', 'MODEL_GW_MASKING_KEY')) {
        if ([string]::IsNullOrWhiteSpace($envValues[$name])) { throw "Set $name in .env." }
    }
    docker compose @compose config --quiet
    Assert-DockerResult 'Compose configuration is invalid.'

    $ownedPorts = docker ps --filter "label=com.docker.compose.project=$ProjectName" --filter 'label=com.docker.compose.service=th-web' --format '{{.Ports}}'
    Assert-DockerResult 'Could not check existing containers.'
    $bindingPattern = '127\.0\.0\.1:' + $Port + '->'
    if (!(($ownedPorts -join ' ') -match $bindingPattern)) {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        try { $listener.Start() }
        catch { throw "Port $Port is occupied. Run the script with -Port <free-port>." }
        finally { $listener.Stop() }
    }

    docker compose @compose up --build --wait --wait-timeout 180 th-workbench th-modelgw th-eval-api
    Assert-DockerResult 'Backend startup failed. Check the service reported by Compose with docker logs.'
    docker compose @compose build demo
    Assert-DockerResult 'Knowledge seed image failed to build.'
    for ($seedAttempt = 1; $seedAttempt -le 3; $seedAttempt++) {
        try {
            $ErrorActionPreference = 'Continue'
            $seedOutput = docker compose @compose run --rm --no-deps demo --seed-kb 2>&1
            $seedResult = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = 'Stop' }
        $seedOutput | ForEach-Object { Write-Host $_ }
        if ($seedResult -eq 0) { break }
        if ($seedAttempt -eq 3 -or ($seedOutput -join "`n") -notmatch 'HTTP 503 /api/v1/versions') {
            throw "Knowledge seed failed. Check docker logs $ProjectName-th-kb-1 and docker logs $ProjectName-th-modelgw-1."
        }
        Start-Sleep -Seconds 5
    }
    docker compose @compose up --build --no-deps --wait --wait-timeout 120 th-gateway th-web
    Assert-DockerResult "Dashboard startup failed. Check docker logs $ProjectName-th-gateway-1 and docker logs $ProjectName-th-web-1."

    $url = "http://127.0.0.1:$Port"
    $health = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $health = Invoke-WebRequest -Uri "$url/healthz" -UseBasicParsing -TimeoutSec 2
            if ($health.StatusCode -eq 200) { break }
        }
        catch { Start-Sleep -Seconds 1 }
    }
    if ($null -eq $health -or $health.StatusCode -ne 200) {
        throw "Dashboard health check failed. Check docker logs $ProjectName-th-web-1."
    }
    Write-Host "TenderHive: $url/"
    Write-Host 'Demo logins: specialist, lawyer, procurement, head, kbadmin.'
    Write-Host "Use DASH_DEMO_PASSWORD from $tenderRuntimeEnv."
}
finally {
    [Environment]::SetEnvironmentVariable('TH_DASHBOARD_PORT', $previousDashboardPort, 'Process')
    Pop-Location
}
