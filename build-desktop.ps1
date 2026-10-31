$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$buildRoot = Join-Path $env:TEMP ("Sweetheart-build-" + [guid]::NewGuid().ToString("N"))
$icon = Join-Path $projectRoot "assets\sweetheart.ico"
$page = Join-Path $projectRoot "public\index.html"
$logo = Join-Path $projectRoot "assets\sweetheart.svg"
$entryPoint = Join-Path $projectRoot "desktop_app.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found. Create it first at .venv."
}

New-Item -ItemType Directory -Path $buildRoot | Out-Null

& $python -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install desktop build dependencies." }

& $python assets\create_icon.py
if ($LASTEXITCODE -ne 0) { throw "Could not create the Sweetheart app icon." }

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name Sweetheart `
    --specpath $buildRoot `
    --workpath (Join-Path $buildRoot "work") `
    --distpath (Join-Path $projectRoot "dist") `
    --icon $icon `
    --add-data "$page;public" `
    --add-data "$logo;assets" `
    --collect-all webview `
    --exclude-module torch `
    --exclude-module transformers `
    --exclude-module peft `
    --exclude-module accelerate `
    $entryPoint
if ($LASTEXITCODE -ne 0) { throw "Desktop app build failed." }

Remove-Item -LiteralPath $buildRoot -Recurse -Force
Write-Output "Desktop app created at: $projectRoot\dist\Sweetheart.exe"
