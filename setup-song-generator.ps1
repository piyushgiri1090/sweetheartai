$ErrorActionPreference = "Stop"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $uv = Join-Path $HOME ".local\bin\uv.exe"
}
if (-not (Test-Path -LiteralPath $uv)) {
    throw "uv is required. Install it with: powershell -ExecutionPolicy ByPass -Command `"irm https://astral.sh/uv/install.ps1 | iex`""
}

$songGeneratorDir = Join-Path $env:LOCALAPPDATA "Sweetheart\SongGenerator"
$engineDir = Join-Path $songGeneratorDir "engine"
$python = Join-Path $songGeneratorDir "venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $songGeneratorDir | Out-Null

& $uv python install 3.12
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 could not be installed."
}

if (-not (Test-Path -LiteralPath $python)) {
    & $uv venv --python 3.12 (Join-Path $songGeneratorDir "venv")
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated song-generator environment could not be created."
    }
}

& $uv pip install --python $python --index-url https://download.pytorch.org/whl/cpu "torch==2.7.1" "torchaudio==2.7.1"
if ($LASTEXITCODE -ne 0) {
    throw "CPU-only PyTorch could not be installed."
}

& $uv pip install --python $python --index-url https://pypi.org/simple -r (Join-Path $PSScriptRoot "requirements-song-generator.txt")
if ($LASTEXITCODE -ne 0) {
    throw "ACE-Step application dependencies could not be installed."
}

if (-not (Test-Path -LiteralPath (Join-Path $engineDir "acestep\api_server.py"))) {
    $temporaryDir = Join-Path $env:TEMP ("sweetheart-ace-step-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporaryDir | Out-Null
    try {
        $archive = Join-Path $temporaryDir "source.zip"
        $extractDir = Join-Path $temporaryDir "source"
        Invoke-WebRequest `
            -Uri "https://api.github.com/repos/ACE-Step/ACE-Step-1.5/zipball/main" `
            -OutFile $archive
        Expand-Archive -LiteralPath $archive -DestinationPath $extractDir
        $sourceDir = Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1
        if (-not $sourceDir -or -not (Test-Path -LiteralPath (Join-Path $sourceDir.FullName "acestep\api_server.py"))) {
            throw "The official ACE-Step source archive did not contain the API server."
        }
        Move-Item -LiteralPath $sourceDir.FullName -Destination $engineDir
    }
    finally {
        Remove-Item -LiteralPath $temporaryDir -Recurse -Force
    }
}

Push-Location $engineDir
try {
    & $python -m acestep.api_server --help
    if ($LASTEXITCODE -ne 0) {
        throw "ACE-Step could not be imported with the CPU-only environment."
    }
}
finally {
    Pop-Location
}

Write-Host "ACE-Step is installed with CPU-only PyTorch."
Write-Host "The song model downloads on the first generation request (about 10 GB)."
