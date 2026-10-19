#!/usr/bin/env bash
#
# Audio Studio MCP installer (Apple Silicon macOS).
#
#   1. checks for uv (offers to install it) and ffmpeg
#   2. clones the official Stable Audio 3 runtime into vendor/ at a pinned commit
#   3. creates .venv/ with a native arm64 Python and installs dependencies
#      (uv sync --extra mlx), then checks that mlx imports
#   4. downloads model weights from Hugging Face (medium: ~5.9 GB)
#   5. prints the command that registers the server with Claude Code
#
# Usage:
#   ./install.sh                     # medium model (recommended)
#   ./install.sh --model all         # medium + sm-music + sm-sfx
#   ./install.sh --skip-weights      # weights download on first use instead
#   ./install.sh -y                  # don't ask before installing uv
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_URL="https://github.com/Stability-AI/stable-audio-3.git"
# Tested upstream commit. The MLX backend uses upstream internals, so bump this
# only after running the smoke test against the new commit.
UPSTREAM_COMMIT="77e0398c26f7d22d801ec1bb036307e8b231f741"
# AUDIO_STUDIO_SA3_DIR points at your own stable-audio-3 checkout; it is used as is.
UPSTREAM_DIR="${AUDIO_STUDIO_SA3_DIR:-$REPO_DIR/vendor/stable-audio-3}"

if [[ -t 1 ]]; then
    B=$'\033[1m'; C=$'\033[1;36m'; R=$'\033[1;31m'; Y=$'\033[1;33m'; G=$'\033[1;32m'; N=$'\033[0m'
else
    B=""; C=""; R=""; Y=""; G=""; N=""
fi
step() { printf '\n%s→ %s%s\n' "$C" "$1" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '%swarning%s: %s\n' "$Y" "$N" "$1" >&2; }
die()  { printf '%serror%s: %s\n' "$R" "$N" "$1" >&2; exit 1; }

ASSUME_YES=0
MODEL="medium"
SKIP_WEIGHTS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1; shift ;;
        --model) [[ $# -ge 2 ]] || die "--model needs a value (medium, sm-music, sm-sfx or all)"
                 MODEL="$2"; shift 2 ;;
        --model=*) MODEL="${1#--model=}"; shift ;;
        --skip-weights) SKIP_WEIGHTS=1; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

for m in ${MODEL//,/ }; do
    case "$m" in medium|sm-music|sm-sfx|all) ;; *) die "unknown model '$m' (choose medium, sm-music, sm-sfx or all)" ;; esac
done

# ── platform ─────────────────────────────────────────────────────────────────
if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" != "arm64" \
      && "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" == "1" ]]; then
    die "this shell runs under Rosetta (x86_64) on an Apple Silicon Mac. Run the installer natively: arch -arm64 ./install.sh"
fi
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    die "this installer supports Apple Silicon macOS (on Windows with an NVIDIA GPU use install.ps1; detected $(uname -s)/$(uname -m))."
fi

# ── uv ───────────────────────────────────────────────────────────────────────
step "Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
    if [[ "$ASSUME_YES" -ne 1 ]]; then
        printf '  uv is required. Install it now with the official installer? [Y/n] '
        if ! { read -r reply < /dev/tty; } 2>/dev/null; then
            printf '\n'
            die "uv is required. Install it (brew install uv) or re-run with -y to install it automatically."
        fi
        case "$reply" in ""|y|Y|yes|YES) ;; *) die "install uv first: brew install uv" ;; esac
    fi
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new shell and re-run."
fi
ok "uv $(uv --version | awk '{print $2}')"

# ── ffmpeg ───────────────────────────────────────────────────────────────────
step "Checking for ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
    ok "ffmpeg found"
else
    warn "ffmpeg not found. Generation works, but process_audio needs it for resampling"
    warn "and loudness normalization. Install it with:  brew install ffmpeg"
fi

# ── upstream runtime ─────────────────────────────────────────────────────────
step "Fetching the Stable Audio 3 MLX runtime (${UPSTREAM_COMMIT:0:7})"
if [[ -n "${AUDIO_STUDIO_SA3_DIR:-}" ]]; then
    [[ -f "$UPSTREAM_DIR/optimized/mlx/scripts/sa3_gradio.py" ]] \
        || die "AUDIO_STUDIO_SA3_DIR=$UPSTREAM_DIR is not a stable-audio-3 checkout."
    if [[ "$(git -C "$UPSTREAM_DIR" rev-parse HEAD 2>/dev/null || true)" != "$UPSTREAM_COMMIT" ]]; then
        warn "$UPSTREAM_DIR is not at the tested commit ${UPSTREAM_COMMIT:0:7}; leaving it as is."
    fi
elif [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
    mkdir -p "$(dirname "$UPSTREAM_DIR")"
    git clone --filter=blob:none --no-checkout "$UPSTREAM_URL" "$UPSTREAM_DIR"
fi
if [[ -z "${AUDIO_STUDIO_SA3_DIR:-}" ]] && {
      [[ "$(git -C "$UPSTREAM_DIR" rev-parse HEAD 2>/dev/null || true)" != "$UPSTREAM_COMMIT" ]] \
      || [[ ! -f "$UPSTREAM_DIR/optimized/mlx/scripts/sa3_gradio.py" ]]; }; then
    if [[ -f "$UPSTREAM_DIR/optimized/mlx/scripts/sa3_gradio.py" \
          && -n "$(git -C "$UPSTREAM_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)" ]]; then
        die "$UPSTREAM_DIR has local changes; commit/stash them or remove the folder."
    fi
    git -C "$UPSTREAM_DIR" cat-file -e "$UPSTREAM_COMMIT^{commit}" 2>/dev/null \
        || git -C "$UPSTREAM_DIR" fetch --quiet origin "$UPSTREAM_COMMIT" \
        || git -C "$UPSTREAM_DIR" fetch --quiet origin
    git -C "$UPSTREAM_DIR" -c advice.detachedHead=false checkout --quiet --force "$UPSTREAM_COMMIT"
fi
ok "$UPSTREAM_DIR @ ${UPSTREAM_COMMIT:0:7}"

# ── Python environment ───────────────────────────────────────────────────────
step "Installing Python dependencies into .venv"
# Ask uv for an arm64 Python explicitly: otherwise it may pick an x86_64 one
# (e.g. an old Intel Homebrew under /usr/local), which runs under Rosetta and
# silently skips mlx. A UV_PYTHON set by the user wins.
if [[ -n "${UV_PYTHON:-}" ]]; then
    (cd "$REPO_DIR" && uv sync --extra mlx)
else
    if [[ -e "$REPO_DIR/.venv" ]] \
          && [[ "$("$REPO_DIR/.venv/bin/python" -c 'import platform; print(platform.machine())' 2>/dev/null || true)" != "arm64" ]]; then
        warn "the existing .venv is not a native arm64 Python environment; recreating it."
        rm -rf "$REPO_DIR/.venv"
    fi
    PY_VERSION="$(tr -d '[:space:]' < "$REPO_DIR/.python-version" 2>/dev/null || true)"
    (cd "$REPO_DIR" && uv sync --extra mlx --python "cpython-${PY_VERSION:-3.11}-macos-aarch64")
fi
VENV_PY="$REPO_DIR/.venv/bin/python"
VENV_ARCH="$("$VENV_PY" -c 'import platform; print(platform.machine())' 2>/dev/null || true)"
if [[ "$VENV_ARCH" != "arm64" ]]; then
    printf '%serror%s: .venv uses an %s Python (%s), but MLX needs a native arm64 Python.\n' \
        "$R" "$N" "${VENV_ARCH:-unknown}" "$("$VENV_PY" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || echo "$VENV_PY")" >&2
    if [[ -n "${UV_PYTHON:-}" ]]; then
        printf '  UV_PYTHON=%s selects it. Unset UV_PYTHON (or set it to cpython-3.11-macos-aarch64)\n' "$UV_PYTHON" >&2
        printf '  and re-run ./install.sh.\n' >&2
    else
        printf '  Re-run with an arm64 Python: UV_PYTHON=cpython-3.11-macos-aarch64 ./install.sh\n' >&2
    fi
    exit 1
fi
"$VENV_PY" -c 'import mlx.core' 2>/dev/null \
    || die "mlx failed to import in .venv. Remove .venv and re-run ./install.sh."
ok ".venv ready (arm64, mlx)"

# ── weights ──────────────────────────────────────────────────────────────────
if [[ "$SKIP_WEIGHTS" -eq 1 ]]; then
    step "Skipping weights"
    warn "the first generation will download them (medium: ~5.9 GB) and can take a long time."
else
    step "Downloading model weights: $MODEL (from huggingface.co/stabilityai/stable-audio-3-optimized)"
    printf '  The weights are licensed under the Stability AI Community License.\n'
    AUDIO_STUDIO_SA3_DIR="$UPSTREAM_DIR" "$REPO_DIR/.venv/bin/python" \
        "$REPO_DIR/scripts/download_weights.py" ${MODEL//,/ }
fi

# ── done ─────────────────────────────────────────────────────────────────────
BIN="$REPO_DIR/.venv/bin/audio-studio-mcp"
ENV_ARG=""
[[ -n "${AUDIO_STUDIO_SA3_DIR:-}" ]] && ENV_ARG="-e \"AUDIO_STUDIO_SA3_DIR=$UPSTREAM_DIR\" "
printf '\n%s%s✓ Installed.%s Register it with Claude Code:\n\n' "$B" "$G" "$N"
printf '  claude mcp add --scope user audio-studio %s-- "%s"\n\n' "$ENV_ARG" "$BIN"
printf 'Other MCP clients (Claude Desktop, Cursor, …): command = %s\n' "$BIN"
printf 'Test it:  "%s/.venv/bin/python" "%s/scripts/smoke_test.py"\n' "$REPO_DIR" "$REPO_DIR"
