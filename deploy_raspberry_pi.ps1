#!/usr/bin/env pwsh
<#
.SYNOPSIS
Builds and deploys KasaBasementBridge to a Raspberry Pi over SSH/SCP.

.DESCRIPTION
Reads deployment settings from deploy_config.json (if present), then:
1) Runs local ARM build (unless -SkipBuild is used)
2) Stops remote systemd service
3) Uploads executable and templates
4) Makes executable, restarts service, prints status

To configure for your setup, copy deploy_config.example.json to deploy_config.json
and edit the values.

.EXAMPLE
.\deploy_raspberry_pi.ps1

.EXAMPLE
.\deploy_raspberry_pi.ps1 -PiHost raspberrypi.local -PiUser pi -DryRun

.EXAMPLE
.\deploy_raspberry_pi.ps1 -SkipBuild -NoTemplateSync
#>

[CmdletBinding()]
param(
    [string]$PiHost,
    [string]$PiUser,
    [string]$RemoteDir,
    [string]$ServiceName,
    [string]$BuildScript = ".\build_docker_desktop.ps1",
    [string]$ExecutablePath = ".\dist\KasaBasementBridge",
    [string]$SshKeyPath,
    [switch]$AllowInteractiveSsh,
    [switch]$SkipBuild,
    [switch]$NoTemplateSync,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Load config from deploy_config.json if it exists
$configPath = Join-Path $PSScriptRoot "deploy_config.json"
$config = @{
    pi_host = "raspberrypi.local"
    pi_user = "pi"
    remote_dir = "~/KasaBasement"
    service_name = "kasabasement"
    ssh_key_path = ""
}

if (Test-Path $configPath) {
    Write-Host "Loading config from deploy_config.json" -ForegroundColor Gray
    $loadedConfig = Get-Content $configPath | ConvertFrom-Json
    if ($loadedConfig.pi_host) { $config.pi_host = $loadedConfig.pi_host }
    if ($loadedConfig.pi_user) { $config.pi_user = $loadedConfig.pi_user }
    if ($loadedConfig.remote_dir) { $config.remote_dir = $loadedConfig.remote_dir }
    if ($loadedConfig.service_name) { $config.service_name = $loadedConfig.service_name }
    if ($loadedConfig.ssh_key_path) { $config.ssh_key_path = $loadedConfig.ssh_key_path }
} else {
    Write-Host "No deploy_config.json found, using defaults. Copy deploy_config.example.json to deploy_config.json to configure." -ForegroundColor Yellow
}

# Command-line parameters override config file
if (-not $PiHost) { $PiHost = $config.pi_host }
if (-not $PiUser) { $PiUser = $config.pi_user }
if (-not $RemoteDir) { $RemoteDir = $config.remote_dir }
if (-not $ServiceName) { $ServiceName = $config.service_name }
if (-not $SshKeyPath) { $SshKeyPath = $config.ssh_key_path }

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Local {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    if ($DryRun) {
        $cmd = @($FilePath) + $ArgumentList
        Write-Host "[DRYRUN] $($cmd -join ' ')"
        return
    }
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($ArgumentList -join ' ')"
    }
}

function Invoke-Remote {
    param([string]$RemoteCommand)
    $target = "$PiUser@$PiHost"
    $args = @()
    if ($SshKeyPath) {
        $args += @("-i", $SshKeyPath)
    }
    if (-not $AllowInteractiveSsh) {
        $args += @("-o", "BatchMode=yes")
    }
    $args += @($target, $RemoteCommand)
    Invoke-Local -FilePath "ssh" -ArgumentList $args
}

function Copy-ToRemote {
    param(
        [string]$Source,
        [string]$Destination,
        [switch]$Recursive
    )
    $target = "$PiUser@$PiHost`:$Destination"
    $args = @()
    if ($SshKeyPath) {
        $args += @("-i", $SshKeyPath)
    }
    if (-not $AllowInteractiveSsh) {
        # Pass ssh options through scp's -o
        $args += @("-o", "BatchMode=yes")
    }
    if ($Recursive) { $args += "-r" }
    $args += @($Source, $target)
    Invoke-Local -FilePath "scp" -ArgumentList $args
}

function Assert-CommandAvailable {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found in PATH: $Name"
    }
}

Write-Step "Checking prerequisites"
Assert-CommandAvailable -Name "ssh"
Assert-CommandAvailable -Name "scp"
if ($SshKeyPath -and -not (Test-Path -LiteralPath $SshKeyPath)) {
    throw "SSH key file not found: $SshKeyPath"
}

if (-not $SkipBuild) {
    Write-Step "Building ARM executable via $BuildScript"
    if (-not (Test-Path -LiteralPath $BuildScript)) {
        throw "Build script not found: $BuildScript"
    }
    Invoke-Local -FilePath "pwsh" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $BuildScript)
}
else {
    Write-Step "Skipping build"
}

Write-Step "Validating local artifact"
if (-not (Test-Path -LiteralPath $ExecutablePath)) {
    throw "Executable not found: $ExecutablePath"
}

$target = "$PiUser@$PiHost"
Write-Step "Deploy target: $target"

Write-Step "Stopping remote service ($ServiceName)"
Invoke-Remote "sudo -n systemctl stop $ServiceName || true"

Write-Step "Ensuring remote directory exists"
Invoke-Remote "mkdir -p $RemoteDir"

Write-Step "Uploading executable"
Copy-ToRemote -Source $ExecutablePath -Destination "$RemoteDir/"

if (-not $NoTemplateSync) {
    Write-Step "Uploading templates directory"
    if (-not (Test-Path -LiteralPath ".\templates")) {
        throw "Templates directory not found: .\templates"
    }
    Copy-ToRemote -Source ".\templates" -Destination "$RemoteDir/" -Recursive
}
else {
    Write-Step "Skipping template sync"
}

Write-Step "Applying permissions and restarting service"
$remotePost = @"
set -e
chmod +x $RemoteDir/KasaBasementBridge
sudo -n systemctl daemon-reload
sudo -n systemctl start $ServiceName
sudo -n systemctl --no-pager --full status $ServiceName
"@
Invoke-Remote $remotePost

Write-Step "Deployment complete"
Write-Host "Deployed to $target at $RemoteDir"
