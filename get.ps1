# zcrew one-command installer (Windows PowerShell 5.1 and PowerShell 7).
#
#   irm https://raw.githubusercontent.com/b9b4ymiN/zcrew/main/get.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File get.ps1        (from a checkout)
#
# The script body is piped into Invoke-Expression, so it has no param() block.
# Optional environment overrides:
#   ZCREW_REPO   git URL to clone      (default https://github.com/b9b4ymiN/zcrew.git)
#   ZCREW_REF    branch, tag or commit (default main)
#   ZCREW_HOME   install root          (default $HOME\.zcrew)
#   ZCODE_CLI_BUNDLE  path to ZCode's resources\glm\zcode.cjs if ZCode is installed elsewhere
#   ZCREW_COMMANDER   auto|claude|codex|both - which commander gets the MCP server
#                     (default auto: Claude Code and/or Codex, whichever is installed)
# Testing only (sandboxed end-to-end runs; never needed by users):
#   ZCREW_SOURCE_DIR=<dir>  copy this local directory to ZCREW_HOME\src instead of cloning
#   ZCREW_NO_PATH=1         do not modify the user PATH in the registry
#   ZCREW_SKIP_MCP=1        do not register the MCP server (install.ps1 -SkipMcpRegistration)
#
# Everything runs inside Install-Zcrew so that a failure never closes the
# caller's window: errors are reported and the function returns $false.

function Install-Zcrew {
    $ErrorActionPreference = 'Stop'

    function Get-Setting([string]$Name, [string]$Default) {
        $value = [Environment]::GetEnvironmentVariable($Name)
        if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
        return $value.Trim()
    }

    # Native tools write progress to stderr; under PS 5.1 with 'Stop' that would
    # become a terminating error, so relax it around the call and check the exit code.
    function Invoke-Native([string]$What, [scriptblock]$Block) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $Block | Out-Host } finally { $ErrorActionPreference = $previous }
        if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
    }

    function Find-ZCodeBundle {
        $explicit = $env:ZCODE_CLI_BUNDLE
        if ($explicit -and (Test-Path -LiteralPath $explicit -PathType Leaf)) { return $explicit }
        $roots = @(
            $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\ZCode' }),
            $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles 'ZCode' }),
            $(if ($env:ProgramW6432) { Join-Path $env:ProgramW6432 'ZCode' }),
            $(if ($env:APPDATA) { Join-Path $env:APPDATA 'Programs\ZCode' })
        ) | Where-Object { $_ }
        foreach ($root in $roots) {
            $candidate = Join-Path $root 'resources\glm\zcode.cjs'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
        return $null
    }

    $repo = Get-Setting 'ZCREW_REPO' 'https://github.com/b9b4ymiN/zcrew.git'
    $ref = Get-Setting 'ZCREW_REF' 'main'
    $zcrewHome = Get-Setting 'ZCREW_HOME' (Join-Path $HOME '.zcrew')
    $sourceDir = Get-Setting 'ZCREW_SOURCE_DIR' ''
    $commander = (Get-Setting 'ZCREW_COMMANDER' 'auto').ToLowerInvariant()
    if (@('auto', 'claude', 'codex', 'both') -notcontains $commander) {
        throw "ZCREW_COMMANDER='$commander' is not one of: auto, claude, codex, both."
    }
    $src = Join-Path $zcrewHome 'src'
    $bin = Join-Path $zcrewHome 'bin'

    Write-Host ''
    Write-Host '=== zcrew installer: Claude Code or Codex commands, ZCode workers build ===' -ForegroundColor Cyan
    Write-Host "Install location: $zcrewHome"
    Write-Host "Commander: $commander"
    Write-Host ''

    # a. prerequisites ---------------------------------------------------------
    Write-Host 'Checking prerequisites...'
    $missing = New-Object System.Collections.Generic.List[string]
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        $missing.Add('git: install Git for Windows from https://git-scm.com/download/win (or: winget install Git.Git)')
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        $missing.Add('python 3.10+: install from https://www.python.org/downloads/ and tick "Add python.exe to PATH" (or: winget install Python.Python.3.12)')
    } else {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $pyVersion = & $python.Source -c "import sys; print('%d.%d' % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            $pyExit = $LASTEXITCODE
        } catch {
            $pyVersion = $null
            $pyExit = 1
        } finally { $ErrorActionPreference = $previous }
        if ($pyExit -ne 0) {
            if ($pyVersion) {
                $missing.Add("python 3.10+: found python $pyVersion at $($python.Source); install 3.10 or newer from https://www.python.org/downloads/")
            } else {
                $missing.Add("python 3.10+: '$($python.Source)' does not run (the Microsoft Store alias?). Install from https://www.python.org/downloads/ with ""Add python.exe to PATH"", or turn off the python App execution alias in Windows Settings")
            }
        } else {
            Write-Host "  python $pyVersion"
        }
    }
    $claudeHint = 'claude (Claude Code CLI): see https://docs.claude.com/en/docs/claude-code/setup, then run "claude" once and log in'
    $codexHint = 'codex (Codex CLI): npm install -g @openai/codex, then run "codex" once and sign in'
    $hasClaude = [bool](Get-Command claude -ErrorAction SilentlyContinue)
    $hasCodex = [bool](Get-Command codex -ErrorAction SilentlyContinue)
    if ($commander -eq 'auto') {
        if (-not $hasClaude -and -not $hasCodex) {
            $missing.Add("a commander: zcrew needs Claude Code or Codex (or both). Install one of: $claudeHint | $codexHint")
        }
    } else {
        if (($commander -eq 'claude' -or $commander -eq 'both') -and -not $hasClaude) { $missing.Add($claudeHint) }
        if (($commander -eq 'codex' -or $commander -eq 'both') -and -not $hasCodex) { $missing.Add($codexHint) }
    }
    $found = @()
    if ($hasClaude) { $found += 'claude' }
    if ($hasCodex) { $found += 'codex' }
    $bundle = Find-ZCodeBundle
    if (-not $bundle) {
        $missing.Add('ZCode Desktop: install it from Z.ai and sign in with the GLM Coding Plan. If it is installed in a custom folder, set ZCODE_CLI_BUNDLE to its resources\glm\zcode.cjs')
    } else {
        Write-Host "  ZCode: $bundle"
    }
    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Host 'Cannot install yet. Missing:' -ForegroundColor Yellow
        foreach ($item in $missing) { Write-Host "  - $item" -ForegroundColor Yellow }
        Write-Host ''
        Write-Host 'Install the items above, open a NEW PowerShell window (so PATH is refreshed), and run the installer again.'
        return $false
    }
    $python = $python.Source
    Write-Host "  git, python, ZCode, commander ($($found -join ', ')): OK"

    # b. get the source --------------------------------------------------------
    New-Item -ItemType Directory -Force -Path $zcrewHome | Out-Null
    if ($sourceDir) {
        # Testing only: install the working tree of a local checkout.
        Write-Host "ZCREW_SOURCE_DIR set: copying $sourceDir -> $src (testing mode, no git clone)"
        if (Test-Path -LiteralPath $src) { Remove-Item -Recurse -Force -LiteralPath $src }
        New-Item -ItemType Directory -Force -Path $src | Out-Null
        Get-ChildItem -LiteralPath $sourceDir -Force |
            Where-Object { $_.Name -ne '.git' -and $_.Name -ne '__pycache__' } |
            ForEach-Object { Copy-Item -Recurse -Force -LiteralPath $_.FullName -Destination $src }
    } elseif (Test-Path -LiteralPath (Join-Path $src '.git')) {
        Write-Host "Updating $src to $ref..."
        Invoke-Native 'git remote set-url' { git -C $src remote set-url origin $repo }
        Invoke-Native 'git fetch' { git -C $src fetch --quiet origin $ref }
        Invoke-Native 'git checkout' { git -C $src checkout --quiet $ref }
        Invoke-Native 'git merge --ff-only' { git -C $src merge --ff-only --quiet FETCH_HEAD }
    } elseif (Test-Path -LiteralPath $src) {
        throw "$src exists but is not a git checkout. Delete that folder and run the installer again."
    } else {
        Write-Host "Cloning $repo ($ref) -> $src"
        Invoke-Native 'git clone' { git clone --quiet $repo $src }
        Invoke-Native 'git checkout' { git -C $src checkout --quiet $ref }
    }
    $zcrewPy = Join-Path $src 'scripts\zcrew.py'
    if (-not (Test-Path -LiteralPath $zcrewPy -PathType Leaf)) { throw "$zcrewPy not found; is $repo the zcrew repository?" }

    # c-e. installer, zcrew.cmd shim, user PATH, doctor --------------------------
    # 'zcrew.py _setup' decides whether the ZCode CLI config needs bootstrapping
    # (only when ~/.zcode/cli/config.json has no model.main), runs
    # scripts\install.ps1 in a child 'powershell -ExecutionPolicy Bypass -File',
    # writes $bin\zcrew.cmd, adds $bin to the user PATH and runs the doctor.
    Write-Host ''
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python $zcrewPy _setup --commander $commander | Out-Host
        $setupExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($setupExit -ne 0) { throw "Setup failed (exit $setupExit); see the messages above." }

    $sessionPath = @($env:Path -split ';' | Where-Object { $_ })
    if (-not ($sessionPath | Where-Object { $_.TrimEnd('\') -ieq $bin.TrimEnd('\') })) {
        $env:Path = (@($sessionPath) + $bin) -join ';'
    }

    # f. summary -----------------------------------------------------------------
    Write-Host ''
    Write-Host '=== zcrew installed ===' -ForegroundColor Green
    Write-Host 'Next steps:'
    Write-Host '  1. Quit Claude Code / Codex completely and start it again (it loads the zcode_executor tools at start).'
    Write-Host '  2. In each project you want to use it in:   cd your-project; zcrew enable'
    Write-Host '     (Codex: zcrew enable --commander codex   both: zcrew enable --commander both)'
    Write-Host '  3. Open a NEW Claude Code or Codex session in that project and give it a task.'
    Write-Host ''
    Write-Host 'Other commands: zcrew status | zcrew doctor | zcrew update | zcrew --help'
    Write-Host 'Open a new terminal if "zcrew" is not found in an already-open one.'
    return $true
}

$zcrewOk = $false
try {
    $zcrewOk = Install-Zcrew
} catch {
    Write-Host ''
    Write-Host "zcrew install failed: $($_.Exception.Message)" -ForegroundColor Red
    $zcrewOk = $false
}
# Only a direct 'powershell -File get.ps1' run may set an exit code; under
# 'irm ... | iex' this would close the user's window, so it is skipped there.
$zcrewOk = (@($zcrewOk) | Select-Object -Last 1) -eq $true
if (-not $zcrewOk -and $PSCommandPath -and ((Split-Path -Leaf $PSCommandPath) -ieq 'get.ps1')) { exit 1 }
