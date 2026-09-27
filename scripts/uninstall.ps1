[CmdletBinding()]
param(
    [ValidateSet('user','local','project')]
    [string]$Scope = 'user',
    [switch]$RemoveBridge,
    [switch]$SkipMcpRegistration
)
$ErrorActionPreference = 'Stop'

Write-Host 'Projects enabled with "commander.py enable" import the policy file removed below.'
Write-Host 'Run "zcrew disable <project>" (or "python ~/.zcode-commander/commander.py disable <project>") in each of them first.'
Write-Host 'Projects enabled for Codex keep a copy of the policy in AGENTS.override.md until disabled.'

if ($SkipMcpRegistration) {
    Write-Host 'Skipping Claude/Codex MCP removal (-SkipMcpRegistration).'
} else {
    # "No MCP server named ..." is expected when a commander was never
    # registered; stderr is discarded and the exit code ignored.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if (Get-Command claude -ErrorAction SilentlyContinue) {
            & claude mcp remove zcode_executor --scope $Scope 2>&1 | Out-Null
        }
        if (Get-Command codex -ErrorAction SilentlyContinue) {
            & codex mcp remove zcode_executor 2>&1 | Out-Null
        }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

# v0.1.x imported the policy globally. Remove that line only if it is present,
# so the user's global CLAUDE.md is otherwise never rewritten.
$ClaudeMemory = Join-Path $HOME '.claude\CLAUDE.md'
$ImportLine = '@~/.claude/zcode-commander/COMMANDER.md'
if (Test-Path -LiteralPath $ClaudeMemory) {
    $lines = Get-Content -LiteralPath $ClaudeMemory -Encoding UTF8
    if ($lines | Where-Object { $_.Trim() -eq $ImportLine }) {
        $kept = $lines | Where-Object { $_.Trim() -ne $ImportLine }
        [System.IO.File]::WriteAllLines($ClaudeMemory, [string[]]$kept, (New-Object System.Text.UTF8Encoding $false))
        Write-Host "Removed legacy global import from $ClaudeMemory"
    }
}

$PolicyDir = Join-Path $HOME '.claude\zcode-commander'
if (Test-Path -LiteralPath $PolicyDir) { Remove-Item -Recurse -Force -LiteralPath $PolicyDir }

if ($RemoveBridge) {
    $InstallRoot = Join-Path $HOME '.zcode-commander'
    if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -Recurse -Force -LiteralPath $InstallRoot }
}

if ($SkipMcpRegistration) { Write-Host 'Removed commander policy.' } else { Write-Host 'Removed Claude/Codex MCP registration and commander policy.' }
