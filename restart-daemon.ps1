# Restart the "voice of claude" TTS daemon cleanly.
#
#   ./restart-daemon.ps1                         # stop + relaunch with current env
#   ./restart-daemon.ps1 -Voice en_US-amy-medium # set Piper voice (persist) + restart
#   ./restart-daemon.ps1 -Engine kokoro -Voice af_heart
#
# Handles the two things that make a manual restart fragile: the project path has
# spaces (the script arg must be quoted), and the daemon has no auto-restart.
param(
    [string]$Voice,
    [string]$Engine,
    [string]$Speaker,  # speaker index for multi-speaker Piper voices (e.g. 16)
    [string]$Speed,    # <1.0 = slower words, >1.0 = faster (default 1.0)
    [string]$Gap       # ms of silence between sentences (wider pauses, same word speed)
)
$ErrorActionPreference = "Stop"
$root   = $PSScriptRoot
$py     = Join-Path $root ".venv\Scripts\python.exe"
$pyw    = Join-Path $root ".venv\Scripts\pythonw.exe"
$server = Join-Path $root "src\tts_server.py"
$port   = if ($env:TTS_PORT) { [int]$env:TTS_PORT } else { 7766 }

function Test-Port([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $p); $c.Close(); return $true } catch { return $false }
}

# Persist + apply any voice/engine change so both this launch and future
# sessions pick it up.
if ($Engine)  { setx TTS_ENGINE $Engine | Out-Null; $env:TTS_ENGINE = $Engine; Write-Host "TTS_ENGINE = $Engine" }
if ($Voice) {
    # Route -Voice to the variable the active engine reads: Kokoro uses
    # TTS_VOICE, Piper uses TTS_PIPER_VOICE. Pick the engine -Engine sets, else
    # the current one, else the piper default.
    $targetEngine = if ($Engine) { $Engine } elseif ($env:TTS_ENGINE) { $env:TTS_ENGINE } else { "piper" }
    $voiceVar = if ($targetEngine -eq "kokoro") { "TTS_VOICE" } else { "TTS_PIPER_VOICE" }
    setx $voiceVar $Voice | Out-Null
    Set-Item -Path "Env:$voiceVar" -Value $Voice
    Write-Host "$voiceVar = $Voice"
}
if ($Speaker) { setx TTS_PIPER_SPEAKER $Speaker | Out-Null; $env:TTS_PIPER_SPEAKER = $Speaker; Write-Host "TTS_PIPER_SPEAKER = $Speaker" }
if ($Speed)   { setx TTS_SPEED $Speed | Out-Null; $env:TTS_SPEED = $Speed; Write-Host "TTS_SPEED = $Speed" }
if ($Gap)     { setx TTS_GAP_MS $Gap | Out-Null; $env:TTS_GAP_MS = $Gap; Write-Host "TTS_GAP_MS = $Gap" }

# Stop any running daemon(s).
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*tts_server.py*' } |
    ForEach-Object {
        Write-Host "Stopping daemon PID $($_.ProcessId)" -ForegroundColor DarkGray
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Milliseconds 600   # let the OS release the listening port

# Relaunch detached (pythonw = no console window). Quote the path (spaces!).
$exe = if (Test-Path $pyw) { $pyw } else { $py }
Start-Process -WindowStyle Hidden -FilePath $exe -ArgumentList "`"$server`"" -WorkingDirectory $root
Write-Host "Launching daemon..." -ForegroundColor Yellow

# Wait for it to bind + load the model (model load is ~1-2s).
for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Milliseconds 250
    if (Test-Port $port) {
        Write-Host "Daemon is up and listening on 127.0.0.1:$port" -ForegroundColor Green
        exit 0
    }
}
Write-Host "Daemon did not come up within ~6s. Check .state\tts_server.log" -ForegroundColor Red
exit 1
