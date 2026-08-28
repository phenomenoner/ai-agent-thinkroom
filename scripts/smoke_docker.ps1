param(
    [Parameter(Mandatory = $true)][string]$Context,
    [string]$Image = "thinkroom:release-verification",
    [string]$Container = "thinkroom-release-verification",
    [string]$Network = "thinkroom-release-verification-network",
    [int]$Port = 18788
)

$ErrorActionPreference = "Stop"
$OwnershipLabel = "com.thinkroom.verification"

if ($Container -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') { throw "invalid container name" }
if ($Network -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') { throw "invalid network name" }
if ($Image -notmatch '^[a-z0-9][a-z0-9._/-]{0,127}(:[A-Za-z0-9_.-]{1,128})?$') { throw "invalid image reference" }
if ($Port -lt 1 -or $Port -gt 65535) { throw "invalid host port" }
$Context = (Resolve-Path -LiteralPath $Context -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $Context -PathType Container)) { throw "Docker context is not a directory" }

function Invoke-DockerProbe {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & docker @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return @{ Output = $output; Code = $code }
}

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $result = Invoke-DockerProbe @Arguments
    if ($result.Code -ne 0) {
        throw "docker $($Arguments -join ' ') failed: $($result.Output)"
    }
    return $result.Output
}

function Assert-OwnedResource {
    param(
        [ValidateSet("container", "network", "image")][string]$Kind,
        [string]$Name
    )
    $result = Invoke-DockerProbe $Kind inspect -- $Name
    if ($result.Code -ne 0) { return $false }
    $objects = (($result.Output | Out-String) | ConvertFrom-Json)
    $labels = if ($Kind -eq "network") { $objects[0].Labels } else { $objects[0].Config.Labels }
    if ($labels.$OwnershipLabel -ne "true") {
        throw "refusing to remove unowned $Kind resource: $Name"
    }
    return $true
}

function Remove-TestResources {
    if (Assert-OwnedResource "container" $Container) {
        Invoke-Docker container rm --force -- $Container | Out-Null
    }
    if (Assert-OwnedResource "network" $Network) {
        Invoke-Docker network rm -- $Network | Out-Null
    }
    if (Assert-OwnedResource "image" $Image) {
        Invoke-Docker image rm --force -- $Image | Out-Null
    }
}

Remove-TestResources
try {
    Invoke-Docker build --label "$OwnershipLabel=true" --tag $Image $Context | Out-Null
    $user = (Invoke-Docker image inspect --format "{{.Config.User}}" -- $Image).Trim()
    if ($user -ne "10001:10001") { throw "unexpected image user: $user" }
    Invoke-Docker network create --label "$OwnershipLabel=true" $Network | Out-Null

    Invoke-Docker run --detach --name $Container `
        --label "$OwnershipLabel=true" `
        --network $Network `
        --publish "127.0.0.1:${Port}:8787" `
        --read-only `
        --tmpfs "/data:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0700" `
        --cap-drop ALL `
        --security-opt "no-new-privileges:true" `
        --memory 512m `
        --cpus 1 `
        --pids-limit 128 `
        $Image | Out-Null

    $health = ""
    for ($i = 0; $i -lt 90; $i++) {
        $state = (Invoke-Docker inspect --format "{{.State.Status}} {{.State.Health.Status}}" -- $Container).Trim().Split(" ")
        if ($state[0] -eq "exited") {
            throw "container exited: $((Invoke-Docker logs -- $Container) -join [Environment]::NewLine)"
        }
        $health = $state[1]
        if ($health -eq "healthy") { break }
        Start-Sleep -Seconds 1
    }
    if ($health -ne "healthy") { throw "container did not become healthy: $health" }
    $peerAddress = (Invoke-Docker run --rm --network $Network --entrypoint python $Image `
        -c "import socket; print(socket.gethostbyname('$Container'))").Trim()
    if (-not $peerAddress) { throw "sibling DNS control failed" }
    $peerResult = Invoke-DockerProbe run --rm --network $Network --entrypoint python $Image `
        -c "from urllib.request import Request,urlopen; urlopen(Request('http://${Container}:8787/health/live',headers={'Host':'127.0.0.1'}),timeout=2)"
    if ($peerResult.Code -eq 0) {
        throw "sibling container reached the unauthenticated proxy: $($peerResult.Output)"
    }
    $lockVerification = (Invoke-Docker exec $Container python scripts/verify_locked_runtime.py /app/uv.lock --manifest /app/runtime-lock-manifest.json).Trim()
    $lockResult = $lockVerification | ConvertFrom-Json
    if ($lockResult.status -ne "ok") {
        throw "runtime dependency lock verification failed: $lockVerification"
    }

    $headers = @{ "Content-Type" = "application/json" }
    $body = @{ question = "Should we choose this important option?"; branch_count = 2 } | ConvertTo-Json
    $created = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$Port/api/v1/research" -Headers $headers -Body $body
    if ($created.StatusCode -ne 202 -or -not $created.Headers.Location) { throw "submit contract failed" }
    $jobId = ($created.Content | ConvertFrom-Json).job_id
    $detail = $null
    for ($i = 0; $i -lt 120; $i++) {
        $detail = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:$Port/api/v1/research/$jobId"
        if ($detail.state -in @("succeeded", "failed", "cancelled")) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($detail.state -ne "succeeded") { throw "research failed: $($detail.terminal_error | ConvertTo-Json -Compress)" }
    @{ status = "ok"; health = $health; state = $detail.state; user = $user; sibling = "denied" } | ConvertTo-Json -Compress
}
finally {
    Remove-TestResources
}

if ((Invoke-DockerProbe container inspect -- $Container).Code -eq 0) { throw "container cleanup verification failed" }
if ((Invoke-DockerProbe image inspect -- $Image).Code -eq 0) { throw "image cleanup verification failed" }
if ((Invoke-DockerProbe network inspect -- $Network).Code -eq 0) { throw "network cleanup verification failed" }
@{ cleanup = "verified"; container = $Container; image = $Image } | ConvertTo-Json -Compress
