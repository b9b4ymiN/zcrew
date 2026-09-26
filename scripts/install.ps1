[CmdletBinding()]
param(
    [ValidateSet('user','local','project')]
    [string]$Scope = 'user',
    [switch]$EnsureZCodeCliConfig,
    [switch]$ForceBridgeUpdate
)

$ErrorActionPreference = 'Stop'
$KitRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Join-Path $HOME '.zcode-commander'
$BridgeRoot = Join-Path $InstallRoot 'coder-mcp-bridge'
$PolicyDir = Join-Path $HOME '.claude\zcode-commander'
$PolicyTarget = Join-Path $PolicyDir 'COMMANDER.md'
$ClaudeMemory = Join-Path $HOME '.claude\CLAUDE.md'
$LauncherTarget = Join-Path $InstallRoot 'zcode_bridge_launcher.py'
$DoctorTarget = Join-Path $InstallRoot 'doctor.py'

function Require-Command([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Required command '$Name' was not found in PATH." }
    return $cmd.Source
}

Write-Host '=== Claude Commander -> ZCode Executor setup ==='
$git = Require-Command 'git'
$python = Require-Command 'python'
$claude = Require-Command 'claude'

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path $PolicyDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ClaudeMemory) | Out-Null

if (-not (Test-Path (Join-Path $BridgeRoot '.git'))) {
    Write-Host "Cloning coder-mcp-bridge -> $BridgeRoot"
    & git clone https://github.com/Deslord319/coder-mcp-bridge.git $BridgeRoot
} elseif ($ForceBridgeUpdate) {
    Write-Host 'Updating coder-mcp-bridge...'
    & git -C $BridgeRoot pull --ff-only
} else {
    Write-Host 'coder-mcp-bridge already installed; leaving pinned working tree unchanged.'
}

Copy-Item -Force (Join-Path $KitRoot 'policy\COMMANDER.md') $PolicyTarget
Copy-Item -Force (Join-Path $PSScriptRoot 'zcode_bridge_launcher.py') $LauncherTarget
Copy-Item -Force (Join-Path $PSScriptRoot 'doctor.py') $DoctorTarget

$ImportLine = '@~/.claude/zcode-commander/COMMANDER.md'
if (-not (Test-Path $ClaudeMemory)) {
    Set-Content -Encoding UTF8 -Path $ClaudeMemory -Value "# User instructions`r`n`r`n$ImportLine`r`n"
} else {
    $memoryText = Get-Content -Raw -Path $ClaudeMemory
    if ($memoryText -notmatch [regex]::Escape($ImportLine)) {
        Add-Content -Encoding UTF8 -Path $ClaudeMemory -Value "`r`n# ZCode executor commander policy`r`n$ImportLine`r`n"
    }
}

# Optional provider/config bootstrap. This may copy the locally configured
# provider/API-key material from ZCode Desktop into ~/.zcode/cli/config.json.
if ($EnsureZCodeCliConfig) {
    Write-Host 'Bootstrapping ZCode CLI config from local ZCode Desktop config...'
    $env:ZCODE_CLI_BUNDLE = Join-Path $env:LOCALAPPDATA 'Programs\ZCode\resources\glm\zcode.cjs'
    $env:ZCODE_BINARY = Join-Path $env:LOCALAPPDATA 'Programs\ZCode\ZCode.exe'
    & $python (Join-Path $BridgeRoot 'server.py') --ensure-config
}

# Re-register idempotently. Remove only our own named server.
& claude mcp remove zcode_executor 2>$null | Out-Null

Write-Host "Registering Claude MCP server 'zcode_executor' (scope=$Scope)..."
& $claude mcp add zcode_executor --scope $Scope -- `
    $python $LauncherTarget

Write-Host ''
Write-Host 'Running zero-model-cost doctor...'
& $python $DoctorTarget

Write-Host ''
Write-Host 'Setup complete. Restart Claude Code, then ask naturally, e.g.:'
Write-Host '  > Implement the agreed plan and verify it end-to-end.'
Write-Host 'Claude should delegate implementation to ZCode automatically; no /zcode step is required.'
