<#
.SYNOPSIS
    Installs the portable 'weightless-windows-tester' Antigravity plugin globally and pre-warms
    the isolated runtime virtual environment under ~/.local_desktop_agent.
#>
[CmdletBinding()]
param(
    [string]$DataDir = $(if ($env:DESKTOP_TESTER_DATA_DIR) { $env:DESKTOP_TESTER_DATA_DIR } else { Join-Path $env:USERPROFILE ".local_desktop_agent" })
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "weightless-windows-tester requires Windows 10/11."
}

$RepoRoot = $PSScriptRoot
$PluginDest = Join-Path $env:USERPROFILE ".gemini\config\plugins\weightless-windows-tester"

Write-Host "=== Installing weightless-windows-tester Antigravity Plugin ===" -ForegroundColor Cyan
Write-Host "Source: $RepoRoot"
Write-Host "Plugin Target: $PluginDest"
Write-Host "Runtime Data Dir: $DataDir"

# 0. Stop any stale running MCP server processes so updated code takes effect cleanly
$StaleTargets = @(
    "weightless-windows-tester\server\server.py",
    "weightless-windows-tester\scripts\launcher.py",
    "LocalAgent\server\server.py",
    ".local_desktop_agent\server.py"
)
$RunningPythons = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue
foreach ($proc in $RunningPythons) {
    if (-not $proc.CommandLine) { continue }
    $normCmd = $proc.CommandLine.Replace("/", "\")
    $matched = $false
    foreach ($needle in $StaleTargets) {
        if ($normCmd.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $matched = $true
            break
        }
    }
    if ($matched) {
        Write-Host "[STEP] Stopping stale MCP server process PID $($proc.ProcessId): $($proc.CommandLine)" -ForegroundColor Yellow
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$RunningNodes = Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue
foreach ($nproc in $RunningNodes) {
    if (-not $nproc.CommandLine) { continue }
    $nCmd = $nproc.CommandLine.Replace("/", "\")
    if (
        $nCmd.IndexOf("weightless-windows-tester\sidecars\woolbur", [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $nCmd.IndexOf("LocalAgent\sidecars\woolbur", [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    ) {
        Write-Host "[STEP] Stopping stale Woolbur sidecar process PID $($nproc.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $nproc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 1. Copy plugin files to ~/.gemini/config/plugins/weightless-windows-tester
New-Item -ItemType Directory -Force -Path `
    $PluginDest, `
    (Join-Path $PluginDest "assets"), `
    (Join-Path $PluginDest "agents"), `
    (Join-Path $PluginDest "scripts"), `
    (Join-Path $PluginDest "server"), `
    (Join-Path $PluginDest "sidecars\woolbur"), `
    (Join-Path $PluginDest "skills\weightless-windows-testing") | Out-Null

$RootFiles = @("plugin.json", "mcp_config.json", "requirements.txt", "README.md", "LICENSE")
foreach ($file in $RootFiles) {
    $src = Join-Path $RepoRoot $file
    if (Test-Path $src) {
        Copy-Item -Force $src (Join-Path $PluginDest $file)
    }
}

Copy-Item -Force (Join-Path $RepoRoot "assets\logo.svg") (Join-Path $PluginDest "assets\logo.svg")
Copy-Item -Force (Join-Path $RepoRoot "agents\weightless-windows-tester.md") (Join-Path $PluginDest "agents\weightless-windows-tester.md")
Copy-Item -Force (Join-Path $RepoRoot "scripts\launcher.py") (Join-Path $PluginDest "scripts\launcher.py")
Copy-Item -Force (Join-Path $RepoRoot "server\*.py") (Join-Path $PluginDest "server\")
Copy-Item -Force (Join-Path $RepoRoot "sidecars\woolbur\*") (Join-Path $PluginDest "sidecars\woolbur\")
Copy-Item -Force (Join-Path $RepoRoot "skills\weightless-windows-testing\SKILL.md") (Join-Path $PluginDest "skills\weightless-windows-testing\SKILL.md")

Write-Host "[OK] Plugin bundle installed to $PluginDest" -ForegroundColor Green

# 2. Pre-warm ~/.local_desktop_agent/.venv and dependencies
$VenvDir = Join-Path $DataDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$MarkerFile = Join-Path $VenvDir ".deps_ok_v1"
$ReqFile = Join-Path $PluginDest "requirements.txt"

New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir "screenshots"), (Join-Path $DataDir "logs") | Out-Null

if (-not (Test-Path $VenvPython)) {
    Write-Host "[STEP] Creating isolated Python virtual environment at $VenvDir..."
    python -m venv $VenvDir
}

if (-not (Test-Path $MarkerFile)) {
    Write-Host "[STEP] Installing Python dependencies..."
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install --quiet -r $ReqFile
    [System.IO.File]::WriteAllText($MarkerFile, "ok`n")
}

Write-Host "[OK] Runtime environment ready at $DataDir" -ForegroundColor Green

# 3. Verify portable launcher MCP stdio handshake
Write-Host "[STEP] Verifying MCP stdio handshake via scripts/launcher.py..."
$LauncherPy = Join-Path $PluginDest "scripts\launcher.py"
$TestScript = @"
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = os.environ.copy()
    env['DESKTOP_TESTER_DATA_DIR'] = r'$DataDir'
    env['DESKTOP_TESTER_NO_STARTUP_BUBBLE'] = '1'
    params = StdioServerParameters(
        command='python',
        args=[r'$LauncherPy'],
        env=env,
        cwd=r'$PluginDest',
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init_res = await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print('Initialized:', init_res.server_info.name, '| Tools (' + str(len(names)) + '):', ', '.join(names))
            assert (
                'mouse_click' in names
                and 'batch_clicks' in names
                and 'set_physical_mouse_lock' in names
                and 'show_status_bubble' in names
                and 'ask_woolbur' in names
                and 'get_app_memory' in names
                and 'update_app_memory' in names
            ), f'Missing tools: {names}'

asyncio.run(main())
"@

& $VenvPython -c $TestScript
Write-Host "[OK] Installation and verification complete!" -ForegroundColor Green
