[CmdletBinding()]
param(
    [ValidateSet('user','local','project')]
    [string]$Scope = 'user',
    [switch]$RemoveBridge
)
$ErrorActionPreference = 'Stop'

try { claude mcp remove zcode_executor | Out-Null } catch {}

$ClaudeMemory = Join-Path $HOME '.claude\CLAUDE.md'
$ImportLine = '@~/.claude/zcode-commander/COMMANDER.md'
if (Test-Path $ClaudeMemory) {
    $lines = Get-Content $ClaudeMemory | Where-Object { $_.Trim() -ne $ImportLine }
    Set-Content -Encoding UTF8 -Path $ClaudeMemory -Value $lines
}

$PolicyDir = Join-Path $HOME '.claude\zcode-commander'
if (Test-Path $PolicyDir) { Remove-Item -Recurse -Force $PolicyDir }

if ($RemoveBridge) {
    $InstallRoot = Join-Path $HOME '.zcode-commander'
    if (Test-Path $InstallRoot) { Remove-Item -Recurse -Force $InstallRoot }
}

Write-Host 'Removed Claude MCP registration and commander policy.'
