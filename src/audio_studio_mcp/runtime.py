"""Where things live, and fetching the upstream Stable Audio 3 runtime.

Two ways to run Audio Studio MCP:

* From a source checkout (install.sh / install.ps1 / uv sync): the upstream
  runtime is cloned into <repo>/vendor/stable-audio-3 and audio goes to
  <repo>/outputs, as before.
* From the published package (uvx audio-studio-mcp, pip install): there is no
  repo folder, so the runtime is downloaded on first use into a per-user data
  folder (see data_home) and audio goes to ~/AudioStudio.

AUDIO_STUDIO_SA3_DIR (your own stable-audio-3 checkout) and
AUDIO_STUDIO_OUTPUT_DIR override either default.

The upstream runtime (github.com/Stability-AI/stable-audio-3, MIT) is not a
pip package: the MLX runtime is a folder of scripts and the PyTorch one is not
on PyPI. We fetch the source archive of one pinned, tested commit and keep only
the two parts the backends use (optimized/mlx and stable_audio_3, ~1 MB).
"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

UPSTREAM_REPO = "Stability-AI/stable-audio-3"
# Tested upstream commit. The MLX backend uses upstream internals, so bump this
# (here, in install.sh and in install.ps1) only after running the smoke test
# against the new commit.
UPSTREAM_COMMIT = "77e0398c26f7d22d801ec1bb036307e8b231f741"
UPSTREAM_ARCHIVE = f"https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/{UPSTREAM_COMMIT}"
# Parts of the upstream tree the backends need (plus its license).
UPSTREAM_KEEP = ("LICENSE", "README.md", "optimized/mlx/", "stable_audio_3/")
MLX_MARKER = "optimized/mlx/scripts/sa3_gradio.py"
TORCH_MARKER = "stable_audio_3/model.py"

# The command line users run (README, server.json). --python: uvx may pick the
# newest Python, which torch 2.7.1 (Windows) has no wheels for. --torch-backend:
# PyPI only has CPU builds of torch for Windows.
UVX_MAC = "uvx --python 3.11 audio-studio-mcp"
UVX_MAC_ARM64 = "uvx --python cpython-3.11-macos-aarch64 audio-studio-mcp"
UVX_NVIDIA = "uvx --python 3.11 --torch-backend cu126 audio-studio-mcp"
UVX_LINUX = 'uvx --python 3.11 --torch-backend cu126 --from "audio-studio-mcp[cuda]" audio-studio-mcp'

PACKAGE_DIR = Path(__file__).resolve().parent


class SetupRequired(RuntimeError):
    """The runtime or the model weights are not installed yet; the message says how."""


def source_checkout() -> Path | None:
    """The repo folder when running from a source checkout (incl. editable installs)."""
    repo = PACKAGE_DIR.parents[1]  # <repo>/src/audio_studio_mcp → <repo>
    if (repo / "pyproject.toml").is_file() and (repo / "src" / "audio_studio_mcp").is_dir():
        return repo
    return None


def data_home() -> Path:
    """Per-user folder for the downloaded runtime (not the weights: those stay in
    the Hugging Face cache, shared with other tools)."""
    if os.environ.get("AUDIO_STUDIO_HOME"):
        # absolute: the MLX backend chdirs, and setup and the client may start elsewhere
        return Path(os.environ["AUDIO_STUDIO_HOME"]).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "audio-studio-mcp"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "audio-studio-mcp"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "audio-studio-mcp"


def managed_runtime() -> bool:
    """True when the upstream runtime is ours to download (package installs)."""
    return not os.environ.get("AUDIO_STUDIO_SA3_DIR") and source_checkout() is None


def upstream_dir() -> Path:
    if os.environ.get("AUDIO_STUDIO_SA3_DIR"):
        return Path(os.environ["AUDIO_STUDIO_SA3_DIR"]).expanduser().resolve()
    repo = source_checkout()
    if repo is not None:
        return repo / "vendor" / "stable-audio-3"
    return data_home() / f"stable-audio-3-{UPSTREAM_COMMIT[:12]}"


def output_dir() -> Path:
    """AUDIO_STUDIO_OUTPUT_DIR, else <repo>/outputs (checkout) or ~/AudioStudio (package)."""
    if os.environ.get("AUDIO_STUDIO_OUTPUT_DIR"):
        return Path(os.environ["AUDIO_STUDIO_OUTPUT_DIR"]).expanduser().resolve()
    repo = source_checkout()
    return repo / "outputs" if repo is not None else Path.home() / "AudioStudio"


def nvidia_gpu_present() -> bool:
    return shutil.which("nvidia-smi") is not None


def cpu_torch_message(version: str) -> str:
    return (f"PyTorch {version} is the CPU-only build, but this machine has an NVIDIA GPU. PyPI only "
            "has CPU builds of PyTorch for Windows: start Audio Studio with --torch-backend cu126 "
            f"(uv 0.9.20 or newer), here and in your MCP client's config:\n  {UVX_NVIDIA} setup")


def run_command() -> str:
    """How this install is started, for messages: the uvx line or the venv's script."""
    repo = source_checkout()
    if repo is not None:
        exe = "audio-studio-mcp.exe" if os.name == "nt" else "audio-studio-mcp"
        scripts = Path(sys.executable).parent
        return f'"{scripts / exe}"' if (scripts / exe).exists() else "audio-studio-mcp"
    if sys.platform == "darwin":
        return UVX_MAC
    if os.name == "nt":
        return UVX_NVIDIA
    return UVX_LINUX


def setup_command(model: str | None = None) -> str:
    extra = f" --model {model}" if model and model != "medium" else ""
    return f"{run_command()} setup{extra}"


def custom_folders() -> str:
    """Folders this process was told about that a plain terminal may not know
    (MCP clients pass env vars); setup must use the same ones."""
    return ", ".join(f"{k}={os.environ[k]}" for k in ("AUDIO_STUDIO_HOME", "HF_HOME", "HF_HUB_CACHE")
                     if os.environ.get(k))


def runtime_ready(path: Path | None = None) -> bool:
    path = path or upstream_dir()
    return (path / MLX_MARKER).is_file() and (path / TORCH_MARKER).is_file()


def _keep(name: str) -> str | None:
    """Archive member → path inside the runtime folder, or None to skip it."""
    parts = PurePosixPath(name).parts
    if (len(parts) < 2 or any(p in ("..", "") for p in parts) or name.startswith("/")
            or "\\" in name or ":" in name):  # no Windows separators or drive letters
        return None
    rel = "/".join(parts[1:])  # drop the "stable-audio-3-<sha>/" prefix
    if any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in UPSTREAM_KEEP):
        return rel
    return None


def _ssl_context():
    """The OS trust store when truststore is installed (corporate proxies), else Python's default."""
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


def fetch_upstream(dest: Path | None = None, log=None) -> Path:
    """Download the pinned upstream commit into dest (atomic: a half-finished
    download never looks like a runtime). Returns dest."""
    import urllib.error
    import urllib.request

    dest = dest or upstream_dir()
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if runtime_ready(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading the Stable Audio 3 runtime ({UPSTREAM_REPO} @ {UPSTREAM_COMMIT[:7]}, ~30 MB)")
    with tempfile.TemporaryDirectory(prefix=".sa3-download-", dir=dest.parent) as tmp:
        archive = Path(tmp) / "src.tar.gz"
        req = urllib.request.Request(UPSTREAM_ARCHIVE, headers={"User-Agent": "audio-studio-mcp"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r, \
                    archive.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        except (urllib.error.URLError, OSError) as e:
            raise SetupRequired(
                f"could not download the Stable Audio 3 runtime from {UPSTREAM_ARCHIVE}: {e}. "
                "Check the internet connection (and any proxy: HTTPS_PROXY) and try again.") from e

        staging = Path(tmp) / "runtime"
        try:
            tf = tarfile.open(archive, "r:gz")
        except tarfile.TarError as e:
            raise SetupRequired(f"the download from {UPSTREAM_ARCHIVE} is not a valid archive "
                                f"({e}); run setup again.") from e
        with tf:
            # git archive records the commit; make sure we got the one we pinned.
            got = (tf.pax_headers or {}).get("comment", "")
            if got != UPSTREAM_COMMIT:
                raise SetupRequired(f"the downloaded runtime is commit {got[:12]}, "
                                    f"expected {UPSTREAM_COMMIT[:12]}")
            for member in tf.getmembers():
                rel = _keep(member.name)
                if rel is None or not (member.isfile() or member.isdir()):
                    continue  # no links or devices; nothing outside the kept parts
                target = staging / rel
                if not target.resolve().is_relative_to(staging.resolve()):
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
        (staging / ".audio-studio-runtime").write_text(
            f"{UPSTREAM_REPO}@{UPSTREAM_COMMIT}\n", encoding="utf-8")
        if not runtime_ready(staging):
            raise SetupRequired(f"the downloaded archive {UPSTREAM_ARCHIVE} has no Stable Audio 3 runtime")
        if dest.exists() and not runtime_ready(dest):
            # an incomplete folder (files deleted or quarantined): replace it
            shutil.rmtree(dest, ignore_errors=True)
        for attempt in range(5):
            try:
                staging.rename(dest)
                break
            except OSError as e:
                if runtime_ready(dest):  # another process won the race
                    break
                if attempt == 4:  # e.g. antivirus still scanning the new files on Windows
                    raise SetupRequired(f"could not move the runtime into {dest}: {e}. "
                                        f"Delete {dest} and run setup again.") from e
                time.sleep(1)
    return dest


def ensure_runtime(log=None) -> Path:
    """The upstream runtime folder; downloads it for package installs."""
    path = upstream_dir()
    if runtime_ready(path):
        return path
    if managed_runtime():
        return fetch_upstream(path, log)
    if os.environ.get("AUDIO_STUDIO_SA3_DIR"):
        raise SetupRequired(f"AUDIO_STUDIO_SA3_DIR={path} is not a stable-audio-3 checkout "
                            "(unset it to let Audio Studio download the runtime).")
    installer = "install.ps1" if os.name == "nt" else "install.sh"
    raise SetupRequired(f"Stable Audio 3 runtime not found at {path}. Run ./{installer} in the "
                        "repository, or set AUDIO_STUDIO_SA3_DIR to a stable-audio-3 checkout.")
