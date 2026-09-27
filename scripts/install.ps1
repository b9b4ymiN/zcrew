[CmdletBinding()]
param(
    [ValidateSet('user','local','project')]
    [string]$Scope = 'user',
    [switch]$EnsureZCodeCliConfig,
    [switch]$ForceBridgeUpdate,
    [switch]$SkipMcpRegistration,
    [switch]$SkipDoctor,
    # Which commander CLIs get the zcode_executor MCP server: auto = every one
    # found on PATH (at least one of claude/codex is required).
    [ValidateSet('auto','claude','codex','both')]
    [string]$Commander = 'auto',
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

# Windows PowerShell 5.1 turns native stderr into a terminating error under
# 'Stop' when it is redirected, so relax it and judge by the exit code.
function Invoke-Native([string]$What, [scriptblock]$Block) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Block | Out-Host } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
}

function Find-Cli([string]$Name) {
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if ($cmd) { return $cmd.Source }
    return $null
}

Write-Host '=== zcrew commander -> ZCode Executor setup ==='
$git = Require-Command 'git'
$python = Require-Command 'python'
$claude = Find-Cli 'claude'
$codex = Find-Cli 'codex'
switch ($Commander) {
    'claude' { if (-not $claude) { throw "-Commander claude: the Claude Code CLI 'claude' was not found in PATH." } }
    'codex'  { if (-not $codex) { throw "-Commander codex: the Codex CLI 'codex' was not found in PATH." } }
    'both'   {
        if (-not $claude) { throw "-Commander both: the Claude Code CLI 'claude' was not found in PATH." }
        if (-not $codex) { throw "-Commander both: the Codex CLI 'codex' was not found in PATH." }
    }
    default  {
        if (-not $claude -and -not $codex) {
            throw "Neither Claude Code ('claude') nor Codex ('codex') was found in PATH. zcrew needs at least one of them as the commander; install one, open a new terminal and run the installer again."
        }
    }
}
$useClaude = [bool]$claude -and ($Commander -ne 'codex')
$useCodex = [bool]$codex -and ($Commander -ne 'claude')

& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is required ($python)." }

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
Copy-Item -Force (Join-Path $PSScriptRoot 'codex_config.py') (Join-Path $InstallRoot 'codex_config.py')

# Starter CLAUDE.md / AGENTS.md for 'enable --with-templates'. The folder is
# replaced on every install so removed templates do not linger.
$TemplatesTarget = Join-Path $InstallRoot 'templates'
if (Test-Path -LiteralPath $TemplatesTarget) { Remove-Item -LiteralPath $TemplatesTarget -Recurse -Force }
New-Item -ItemType Directory -Force -Path $TemplatesTarget | Out-Null
Copy-Item -Force -Recurse (Join-Path $KitRoot 'templates\*') $TemplatesTarget

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

if ($SkipMcpRegistration) {
    Write-Host 'Skipping Claude/Codex MCP registration (-SkipMcpRegistration).'
} elseif (-not $useClaude) {
    if ($Commander -eq 'codex') { Write-Host 'Claude MCP registration not requested (-Commander codex).' }
    else { Write-Host "Claude Code ('claude') not found; skipping its MCP registration." }
} else {
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
}

if ($SkipMcpRegistration) {
    # already reported above
} elseif (-not $useCodex) {
    if ($Commander -eq 'claude') { Write-Host 'Codex MCP registration not requested (-Commander claude).' }
    else { Write-Host "Codex ('codex') not found; skipping its MCP registration." }
} else {
    # 'codex mcp add' fails when the name exists, so remove our own server first.
    # It sets no timeouts; codex_config.py then adds them to ~/.codex/config.toml
    # (backed up to config.toml.bak-zcrew-<timestamp> first).
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $codex mcp get zcode_executor 2>&1 | Out-Null
        $codexHasServer = ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($codexHasServer) {
        Invoke-Native 'codex mcp remove' { & $codex mcp remove zcode_executor }
    }
    Write-Host "Registering Codex MCP server 'zcode_executor'..."
    Invoke-Native 'codex mcp add' { & $codex mcp add zcode_executor -- $python $LauncherTarget }
    Invoke-Native 'codex timeouts' { & $python (Join-Path $InstallRoot 'codex_config.py') apply }
}

if (-not $SkipDoctor) {
    Write-Host ''
    Write-Host 'Running zero-model-cost doctor...'
    & $python $DoctorTarget
}

Write-Host ''
Write-Host 'Setup complete. Restart Claude Code and/or Codex, then enable the commander policy per project.'
