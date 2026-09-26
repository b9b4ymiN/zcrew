[CmdletBinding()]
param(
    [ValidateSet('user','local','project')]
    [string]$Scope = 'user',
    [switch]$EnsureZCodeCliConfig,
    [switch]$ForceBridgeUpdate,
    [string]$BridgeRef = '23ecf0a5f3be1916bb856e56fac5d313e07a05c4'
)

$ErrorActionPreference = 'Stop'
$KitRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Join-Path $HOME '.zcode-commander'
$BridgeRoot = Join-Path $InstallRoot 'coder-mcp-bridge'
$PolicyDir = Join-Path $HOME '.claude\zcode-commander'
$PolicyTarget = Join-Path $PolicyDir 'COMMANDER.md'
$LauncherTarget = Join-Path $InstallRoot 'zcode_bridge_launcher.py'
$DoctorTarget = Join-Path $InstallRoot 'doctor.py'

function Require-Command([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Required command '$Name' was not found in PATH." }
    return $cmd.Source
}

function Invoke-Checked([string]$What, [scriptblock]$Block) {
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
}

Write-Host '=== Claude Commander -> ZCode Executor setup ==='
$git = Require-Command 'git'
$python = Require-Command 'python'
$claude = Require-Command 'claude'

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path $PolicyDir | Out-Null

if (-not (Test-Path (Join-Path $BridgeRoot '.git'))) {
    Write-Host "Cloning coder-mcp-bridge -> $BridgeRoot"
    Invoke-Checked 'git clone' { & $git clone --quiet https://github.com/Deslord319/coder-mcp-bridge.git $BridgeRoot }
    Invoke-Checked 'git checkout' { & $git -C $BridgeRoot checkout --quiet $BridgeRef }
} elseif ($ForceBridgeUpdate) {
    Write-Host "Updating coder-mcp-bridge to $BridgeRef..."
    Invoke-Checked 'git fetch' { & $git -C $BridgeRoot fetch --quiet origin }
    Invoke-Checked 'git checkout' { & $git -C $BridgeRoot checkout --quiet $BridgeRef }
} else {
    Write-Host 'coder-mcp-bridge already installed; leaving pinned working tree unchanged.'
}

# The policy is copied only. It is activated per project (see README), never
# imported into the user-global ~/.claude/CLAUDE.md.
Copy-Item -Force (Join-Path $KitRoot 'policy\COMMANDER.md') $PolicyTarget
Copy-Item -Force (Join-Path $PSScriptRoot 'zcode_bridge_launcher.py') $LauncherTarget
Copy-Item -Force (Join-Path $PSScriptRoot 'doctor.py') $DoctorTarget
Copy-Item -Force (Join-Path $PSScriptRoot 'bridge_compat.py') (Join-Path $InstallRoot 'bridge_compat.py')
Copy-Item -Force (Join-Path $PSScriptRoot 'commander.py') (Join-Path $InstallRoot 'commander.py')

# The user config is seeded once and never overwritten on reinstall.
$UserConfig = Join-Path $InstallRoot 'config.json'
if (-not (Test-Path -LiteralPath $UserConfig)) {
    Copy-Item (Join-Path $KitRoot 'config\default-config.json') $UserConfig
    Write-Host "Created default commander config: $UserConfig"
} else {
    Write-Host "Keeping existing commander config: $UserConfig"
}

# Optional provider/config bootstrap. This copies the locally configured
# provider/API-key material from ZCode Desktop into ~/.zcode/cli/config.json
# (the bridge keeps a .bak of the previous file).
if ($EnsureZCodeCliConfig) {
    Write-Host 'Bootstrapping ZCode CLI config from local ZCode Desktop config...'
    $env:PYTHONUTF8 = '1'
    Invoke-Checked 'ensure-config' { & $python (Join-Path $BridgeRoot 'server.py') --ensure-config }
}

# Re-register idempotently. Remove only our own named server. Windows
# PowerShell 5.1 turns redirected native stderr into a terminating error under
# 'Stop', and "No MCP server named ..." on a first install is expected.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $claude mcp remove zcode_executor --scope $Scope 2>&1 | Out-Null
} finally {
    $ErrorActionPreference = $previousPreference
}

Write-Host "Registering Claude MCP server 'zcode_executor' (scope=$Scope)..."
Invoke-Checked 'claude mcp add' { & $claude mcp add zcode_executor --scope $Scope -- $python $LauncherTarget }

Write-Host ''
Write-Host 'Running zero-model-cost doctor...'
& $python $DoctorTarget

Write-Host ''
Write-Host 'Setup complete. Restart Claude Code, then enable the commander policy per project.'
