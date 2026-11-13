# Builds the Windows app (PyInstaller) and the installer (Inno Setup).
#   scripts/build-agent.ps1            -> dist/installer/PCControlDeck-Setup-<ver>.exe
#   scripts/build-agent.ps1 -NoInstaller
#   scripts/build-agent.ps1 -SkipTests     (CI: tests already ran in their own step)
# Linux / macOS: scripts/build-agent.sh
param([switch]$NoInstaller, [switch]$SkipTests)
# Native tools (PyInstaller, ISCC) log to stderr; with "Stop" Windows PowerShell 5.1 turns that
# into a terminating error. Failures are detected through $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$agent = Split-Path -Parent $PSScriptRoot
Push-Location $agent
try {
    $py = ".\.venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { throw "run scripts/setup-agent.ps1 first" }
    $env:PYTHONPATH = "src"
    $version = & $py -c "import pcdeck_agent; print(pcdeck_agent.__version__)"
    Write-Host "== Control Deck $version"

    & $py tools\fetch_lhm.py
    if ($LASTEXITCODE -ne 0) { throw "LibreHardwareMonitor fetch failed" }
    & $py tools\fetch_firmware.py  # the decks' latest firmware, bundled for the USB updater
    if ($LASTEXITCODE -ne 0) { throw "deck firmware fetch failed" }
    if (-not $SkipTests) {
        & $py -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "tests failed" }
    }

    New-Item -ItemType Directory -Force build | Out-Null
    & $py packaging\make_icon.py build\pcdeck.ico
    if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }
    & $py packaging\make_version.py build\version_info.txt
    if ($LASTEXITCODE -ne 0) { throw "version resource generation failed" }

    & $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\pcdeck.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    if (-not $NoInstaller) {
        $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
        & $iscc /Q "/DAppVersion=$version" packaging\installer.iss
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
        Get-ChildItem dist\installer\*.exe | ForEach-Object { Write-Host "installer: $($_.FullName) ($([math]::Round($_.Length/1MB,1)) MB)" }
    }
} finally {
    Pop-Location
}
