<#
Audio Studio MCP installer (Windows + NVIDIA GPU).

  1. checks for git, uv (offers to install it), ffmpeg and an NVIDIA GPU
  2. clones the official Stable Audio 3 runtime into vendor\ at a pinned commit
  3. creates .venv\ and installs dependencies (uv sync --extra cuda)
  4. downloads model weights from Hugging Face (medium: ~10.4 GB). The model
     repos are gated: accept the license on the model page and log in once.
  5. prints the command that registers the server with Claude Code

Usage (from the repo folder):
  powershell -ExecutionPolicy Bypass -File install.ps1                 # medium model (recommended)
  powershell -ExecutionPolicy Bypass -File install.ps1 -Model all      # medium + sm-music + sm-sfx
  powershell -ExecutionPolicy Bypass -File install.ps1 -SkipWeights    # weights download on first use instead
  powershell -ExecutionPolicy Bypass -File install.ps1 -Yes            # don't ask before installing uv
#>
[CmdletBinding()]
param(
    [string]$Model = "medium",
    [switch]$SkipWeights,
    [Alias("y")][switch]$Yes,
    [switch]$Help
)

# Native tools (git, uv) write progress to stderr; check exit codes instead of
# letting Windows PowerShell 5.1 turn stderr lines into terminating errors.
$ErrorActionPreference = "Continue"
Set-StrictMode -Version 2

if ($Help) { Get-Help $PSCommandPath -Detailed; exit 0 }

$RepoDir = $PSScriptRoot
$UpstreamUrl = "https://github.com/Stability-AI/stable-audio-3.git"
# Tested upstream commit; keep in sync with install.sh. Bump only after running
# the smoke test against the new commit.
$UpstreamCommit = "77e0398c26f7d22d801ec1bb036307e8b231f741"
# AUDIO_STUDIO_SA3_DIR points at your own stable-audio-3 checkout; it is used as is.
$CustomUpstream = [bool]$env:AUDIO_STUDIO_SA3_DIR
if ($CustomUpstream) { $UpstreamDir = $env:AUDIO_STUDIO_SA3_DIR } else { $UpstreamDir = Join-Path $RepoDir "vendor\stable-audio-3" }
$Short = $UpstreamCommit.Substring(0, 7)

function Step($msg) { Write-Host ""; Write-Host "-> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Warn($msg) { Write-Host "warning: " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Die($msg)  { Write-Host "error: " -ForegroundColor Red -NoNewline; Write-Host $msg; exit 1 }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
# Stop if the last native command failed.
function Check($what) { if ($LASTEXITCODE -ne 0) { Die "$what failed (exit code $LASTEXITCODE)" } }

$Models = @($Model -split "[,\s]+" | Where-Object { $_ })
foreach ($m in $Models) {
    if (@("medium", "sm-music", "sm-sfx", "all") -notcontains $m) {
        Die "unknown model '$m' (choose medium, sm-music, sm-sfx or all)"
    }
}

# -- platform -----------------------------------------------------------------
if ($env:OS -ne "Windows_NT") { Die "install.ps1 is for Windows; use ./install.sh on macOS." }
if (-not [Environment]::Is64BitOperatingSystem) { Die "64-bit Windows is required." }

# -- git ----------------------------------------------------------------------
Step "Checking for git"
if (-not (Have git)) { Die "git is required: winget install Git.Git (then open a new terminal)" }
Ok ((git --version) -join "")

# -- uv -----------------------------------------------------------------------
Step "Checking for uv"
if (-not (Have uv)) {
    if (-not $Yes) {
        $reply = Read-Host "  uv is required. Install it now with the official installer? [Y/n]"
        if ($reply -and $reply -notmatch "^(y|yes)$") { Die "install uv first: winget install astral-sh.uv" }
    }
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Have uv)) { Die "uv installed but not on PATH; open a new terminal and re-run." }
}
Ok ((uv --version) -join "")

# -- ffmpeg -------------------------------------------------------------------
Step "Checking for ffmpeg"
if (Have ffmpeg) {
    Ok "ffmpeg found"
} else {
    Warn "ffmpeg not found. Generation works, but process_audio needs it for resampling"
    Warn "and loudness normalization. Install it with:  winget install Gyan.FFmpeg"
}

# -- GPU ----------------------------------------------------------------------
Step "Checking for an NVIDIA GPU"
$gpu = $null
if (Have nvidia-smi) {
    $gpu = (nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
}
if ($gpu) {
    Ok $gpu
} else {
    Warn "no NVIDIA GPU / driver found (nvidia-smi). The medium model needs a CUDA GPU with"
    Warn "about 8 GB of VRAM; the small models (-Model sm-music / sm-sfx) also run on the CPU, slowly."
}

# -- upstream runtime ---------------------------------------------------------
Step "Fetching the Stable Audio 3 runtime ($Short)"
$marker = Join-Path $UpstreamDir "stable_audio_3\model.py"
function UpstreamHead {
    $h = git -C $UpstreamDir rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { return "" }
    return ($h -join "").Trim()
}
if ($CustomUpstream) {
    if (-not (Test-Path $marker)) { Die "AUDIO_STUDIO_SA3_DIR=$UpstreamDir is not a stable-audio-3 checkout." }
    if ((UpstreamHead) -ne $UpstreamCommit) { Warn "$UpstreamDir is not at the tested commit $Short; leaving it as is." }
} else {
    if (-not (Test-Path (Join-Path $UpstreamDir ".git"))) {
        New-Item -ItemType Directory -Force (Split-Path $UpstreamDir) | Out-Null
        git clone --filter=blob:none --no-checkout $UpstreamUrl $UpstreamDir; Check "git clone"
    }
    if (((UpstreamHead) -ne $UpstreamCommit) -or -not (Test-Path $marker)) {
        if ((Test-Path $marker) -and (git -C $UpstreamDir status --porcelain --untracked-files=no)) {
            Die "$UpstreamDir has local changes; commit/stash them or remove the folder."
        }
        git -C $UpstreamDir cat-file -e "$UpstreamCommit^{commit}" 2>$null
        if ($LASTEXITCODE -ne 0) {
            git -C $UpstreamDir fetch --quiet origin $UpstreamCommit
            if ($LASTEXITCODE -ne 0) { git -C $UpstreamDir fetch --quiet origin; Check "git fetch" }
        }
        git -C $UpstreamDir -c advice.detachedHead=false checkout --quiet --force $UpstreamCommit; Check "git checkout"
    }
}
Ok "$UpstreamDir @ $Short"

# -- Python environment -------------------------------------------------------
Step "Installing Python dependencies into .venv (PyTorch + CUDA, ~3 GB)"
Push-Location $RepoDir
try { uv sync --extra cuda; Check "uv sync" } finally { Pop-Location }
$Py = Join-Path $RepoDir ".venv\Scripts\python.exe"
$cuda = (& $Py -c "import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')" 2>$null) -join ""
if ($cuda) { Ok ".venv ready (CUDA: $cuda)" } else { Ok ".venv ready"; Warn "PyTorch cannot see a CUDA GPU; generation will run on the CPU." }

# -- weights ------------------------------------------------------------------
$env:PYTHONIOENCODING = "utf-8"
$Hf = Join-Path $RepoDir ".venv\Scripts\hf.exe"
if ($SkipWeights) {
    Step "Skipping weights"
    Warn "the first generation will download them (medium: ~10.4 GB) and can take a long time."
    Warn "The model repos are gated: accept the license and run '$Hf auth login' first."
} else {
    $repos = @{ "medium" = "stable-audio-3-medium"; "sm-music" = "stable-audio-3-small-music"; "sm-sfx" = "stable-audio-3-small-sfx" }
    if ($Models -contains "all") { $names = @("medium", "sm-music", "sm-sfx") } else { $names = $Models }
    Step "Downloading model weights: $($names -join ', ')"
    Write-Host "  The weights are licensed under the Stability AI Community License and the repos are"
    Write-Host "  gated. If you have not yet, open each page, log in and accept the license:"
    foreach ($n in $names) { Write-Host "    https://huggingface.co/stabilityai/$($repos[$n])" }
    & $Hf auth whoami *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Then paste a Hugging Face access token (read) below:"
        Write-Host "    https://huggingface.co/settings/tokens"
        & $Hf auth login
        if ($LASTEXITCODE -ne 0) { Die "Hugging Face login failed; re-run the installer or use -SkipWeights." }
    }
    $env:AUDIO_STUDIO_SA3_DIR = $UpstreamDir
    & $Py (Join-Path $RepoDir "scripts\download_weights.py") @names
    $code = $LASTEXITCODE
    if (-not $CustomUpstream) { Remove-Item Env:\AUDIO_STUDIO_SA3_DIR }
    if ($code -ne 0) { Die "weights download failed (see above). Re-run the installer to resume." }
}

# -- done ---------------------------------------------------------------------
$Bin = Join-Path $RepoDir ".venv\Scripts\audio-studio-mcp.exe"
$EnvArg = ""
if ($CustomUpstream) { $EnvArg = "-e `"AUDIO_STUDIO_SA3_DIR=$UpstreamDir`" " }
Write-Host ""
Write-Host "Installed. " -ForegroundColor Green -NoNewline
Write-Host "Register it with Claude Code:"
Write-Host ""
Write-Host "  claude mcp add --scope user audio-studio $EnvArg-- `"$Bin`""
Write-Host ""
Write-Host "Other MCP clients (Claude Desktop, Cursor, ...): command = $Bin"
Write-Host "Test it:  & `"$Py`" `"$(Join-Path $RepoDir 'scripts\smoke_test.py')`""
