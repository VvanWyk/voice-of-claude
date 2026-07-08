# Setup for "the voice of claude" - local CPU TTS for Claude Code (Windows 11)
# Creates a venv, installs deps, downloads the Kokoro int8 model, and prints the
# hook config to paste into ~/.claude/settings.json.
#
#   Run from this folder:  ./setup.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$models = Join-Path $root "models"

Write-Host "== the voice of claude :: setup ==" -ForegroundColor Cyan

# 1. venv ---------------------------------------------------------------------
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"

# 2. dependencies -------------------------------------------------------------
Write-Host "Installing dependencies (CPU-only, no GPU libs)..." -ForegroundColor Yellow
& $py -m pip install --upgrade pip
& $py -m pip install -r (Join-Path $root "requirements.txt")

# 3a. Piper voice (default engine) -------------------------------------------
$piperDir = Join-Path $models "piper"
New-Item -ItemType Directory -Force -Path $piperDir | Out-Null
$piperVoice = if ($env:TTS_PIPER_VOICE) { $env:TTS_PIPER_VOICE } else { "en_US-lessac-medium" }
if (Test-Path (Join-Path $piperDir "$piperVoice.onnx")) {
    Write-Host "  exists: piper/$piperVoice" -ForegroundColor DarkGray
} else {
    Write-Host "  downloading Piper voice $piperVoice ..." -ForegroundColor Yellow
    & $py -m piper.download_voices $piperVoice --data-dir $piperDir
}

# 3b. Kokoro model (optional engine) -----------------------------------------
$base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
$files = @{
    "kokoro-v1.0.int8.onnx" = "$base/kokoro-v1.0.int8.onnx"
    "voices-v1.0.bin"       = "$base/voices-v1.0.bin"
}
foreach ($name in $files.Keys) {
    $dest = Join-Path $models $name
    if (Test-Path $dest) {
        Write-Host "  exists: $name" -ForegroundColor DarkGray
        continue
    }
    Write-Host "  downloading $name ..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $files[$name] -OutFile $dest
}

# 4. branded launcher exe (tray icon attribution) ------------------------------
Write-Host "Building branded launcher (voice-of-claude.exe) ..." -ForegroundColor Yellow
& $py (Join-Path $root "src\brand_exe.py")

# 5. print hook config --------------------------------------------------------
$pyExe  = (Join-Path $venv "Scripts\python.exe")
$pywExe = (Join-Path $venv "Scripts\pythonw.exe")
$speak  = (Join-Path $root "src\speak.py")
$launch = (Join-Path $root "src\launch_server.py")

function Esc([string]$s) { $s -replace '\\', '\\' }

$snippet = @"
{
  "hooks": {
    "SessionStart": [
      { "matcher": "",
        "hooks": [ { "type": "command",
          "command": "\"$(Esc $pywExe)\" \"$(Esc $launch)\"",
          "timeout": 15 } ] }
    ],
    "Stop": [
      { "matcher": "",
        "hooks": [ { "type": "command",
          "command": "\"$(Esc $pyExe)\" \"$(Esc $speak)\"",
          "timeout": 15 } ] }
    ],
    "Notification": [
      { "matcher": "",
        "hooks": [ { "type": "command",
          "command": "\"$(Esc $pyExe)\" \"$(Esc $speak)\"",
          "timeout": 15 } ] }
    ],
    "PreToolUse": [
      { "matcher": "AskUserQuestion",
        "hooks": [ { "type": "command",
          "command": "\"$(Esc $pyExe)\" \"$(Esc $speak)\"",
          "timeout": 15 } ] }
    ]
  }
}
"@

Write-Host ""
Write-Host "== Setup complete ==" -ForegroundColor Green
Write-Host "Merge this into your Claude Code settings (e.g. $env:USERPROFILE\.claude\settings.json):" -ForegroundColor Cyan
Write-Host ""
Write-Host $snippet
Write-Host ""
Write-Host "Then start a new 'claude' session. See README.md for testing and tuning." -ForegroundColor Cyan
