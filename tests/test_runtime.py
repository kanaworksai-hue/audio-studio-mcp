"""Package installs (uvx): where things live, fetching the upstream runtime,
the weights gate and the setup command. No network: downloads are faked."""
from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from audio_studio_mcp import firstrun, runtime, server, weights

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def package_install(tmp_path, monkeypatch):
    """Pretend to run from the published package: no source checkout."""
    monkeypatch.setattr(runtime, "source_checkout", lambda: None)
    for var in ("AUDIO_STUDIO_SA3_DIR", "AUDIO_STUDIO_OUTPUT_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUDIO_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runtime.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    return tmp_path


def make_archive(files: dict[str, bytes], commit: str = runtime.UPSTREAM_COMMIT,
                 extra: list[tarfile.TarInfo] = ()) -> bytes:
    """A gzipped tar like codeload.github.com returns for a commit."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": commit}) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"stable-audio-3-{commit}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info in extra:
            tf.addfile(info)
    return buf.getvalue()


UPSTREAM_FILES = {
    "LICENSE": b"MIT License\n",
    "README.md": b"# Stable Audio 3\n",
    runtime.MLX_MARKER: b"# gradio app\n",
    "optimized/mlx/scripts/weights.py": b"# weights\n",
    runtime.TORCH_MARKER: b"# model\n",
    "optimized/tensorRT/scripts/tokenizer.json": b"{}",  # big and unused: not kept
    "docs/index.md": b"docs\n",
}


@pytest.fixture
def fake_download(monkeypatch):
    """Serve an archive instead of GitHub; returns the list of requested URLs."""
    state = {"archive": make_archive(UPSTREAM_FILES), "urls": []}

    def urlopen(req, timeout=None, context=None):
        state["urls"].append(req.full_url)
        return io.BytesIO(state["archive"])

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return state


# ── locations ────────────────────────────────────────────────────────────────
def test_checkout_keeps_vendor_and_outputs(monkeypatch):
    monkeypatch.delenv("AUDIO_STUDIO_SA3_DIR", raising=False)
    monkeypatch.delenv("AUDIO_STUDIO_OUTPUT_DIR", raising=False)
    assert runtime.source_checkout() == REPO
    assert runtime.upstream_dir() == REPO / "vendor" / "stable-audio-3"
    assert runtime.output_dir() == REPO / "outputs"
    assert not runtime.managed_runtime()


def test_package_install_uses_user_folders(package_install):
    home = package_install / "home"
    assert runtime.managed_runtime()
    assert runtime.upstream_dir() == home / f"stable-audio-3-{runtime.UPSTREAM_COMMIT[:12]}"
    assert runtime.output_dir() == package_install / "user" / "AudioStudio"
    assert "uvx --python 3.11" in runtime.setup_command() and runtime.setup_command().endswith(" setup")


def test_env_overrides(package_install, monkeypatch):
    monkeypatch.setenv("AUDIO_STUDIO_SA3_DIR", str(package_install / "mine"))
    monkeypatch.setenv("AUDIO_STUDIO_OUTPUT_DIR", str(package_install / "out"))
    assert runtime.upstream_dir() == (package_install / "mine").resolve()
    assert runtime.output_dir() == (package_install / "out").resolve()
    assert not runtime.managed_runtime()
    with pytest.raises(runtime.SetupRequired, match="AUDIO_STUDIO_SA3_DIR"):
        runtime.ensure_runtime()


def test_data_home_per_platform(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIO_STUDIO_HOME", raising=False)
    monkeypatch.setattr(runtime.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    assert runtime.data_home() == tmp_path / "Library" / "Application Support" / "audio-studio-mcp"
    # Fake os.name for runtime only: patching the real one breaks pathlib on Windows
    # (Path() would build a PosixPath there, which raises).
    import os
    import types
    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert runtime.data_home() == tmp_path / "xdg" / "audio-studio-mcp"
    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert runtime.data_home() == tmp_path / "local" / "audio-studio-mcp"


def test_installers_pin_the_same_commit():
    for script in ("install.sh", "install.ps1"):
        text = (REPO / script).read_text(encoding="utf-8")
        assert re.findall(r"[0-9a-f]{40}", text) == [runtime.UPSTREAM_COMMIT], script


# ── fetching the runtime ─────────────────────────────────────────────────────
def test_fetch_keeps_only_the_runtime(package_install, fake_download):
    path = runtime.ensure_runtime(log=lambda m: None)
    assert path == runtime.upstream_dir() and runtime.runtime_ready(path)
    assert fake_download["urls"] == [runtime.UPSTREAM_ARCHIVE]
    assert (path / "LICENSE").read_bytes() == b"MIT License\n"
    assert (path / "optimized/mlx/scripts/weights.py").is_file()
    assert not (path / "optimized/tensorRT").exists() and not (path / "docs").exists()
    assert runtime.UPSTREAM_COMMIT in (path / ".audio-studio-runtime").read_text()
    # no leftovers next to it, and a second call does not download again
    assert [p.name for p in path.parent.iterdir()] == [path.name]
    runtime.ensure_runtime(log=lambda m: None)
    assert len(fake_download["urls"]) == 1


def test_fetch_rejects_another_commit(package_install, fake_download):
    fake_download["archive"] = make_archive(UPSTREAM_FILES, commit="0" * 40)
    with pytest.raises(runtime.SetupRequired, match="expected 77e0398c26f7"):
        runtime.ensure_runtime(log=lambda m: None)
    assert not runtime.upstream_dir().exists()


def test_fetch_ignores_links_and_traversal(package_install, fake_download):
    link = tarfile.TarInfo(f"stable-audio-3-{runtime.UPSTREAM_COMMIT}/stable_audio_3/evil")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    dotdot = tarfile.TarInfo(f"stable-audio-3-{runtime.UPSTREAM_COMMIT}/stable_audio_3/../../x")
    fake_download["archive"] = make_archive(UPSTREAM_FILES, extra=[link, dotdot])
    path = runtime.ensure_runtime(log=lambda m: None)
    assert not (path / "stable_audio_3" / "evil").exists()
    assert not (path.parent / "x").exists() and not (path / "x").exists()


def test_fetch_failure_is_actionable(package_install, monkeypatch):
    import urllib.error
    import urllib.request

    def offline(*a, **k):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(runtime.SetupRequired, match="internet connection"):
        runtime.ensure_runtime(log=lambda m: None)


def test_checkout_without_vendor_points_to_installer(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIO_STUDIO_SA3_DIR", raising=False)
    monkeypatch.setattr(runtime, "source_checkout", lambda: tmp_path)
    with pytest.raises(runtime.SetupRequired, match=r"install\.(sh|ps1)"):
        runtime.ensure_runtime()


# ── weights ──────────────────────────────────────────────────────────────────
def test_mlx_missing_weights(tmp_path):
    up = tmp_path / "sa3"
    scripts = up / "optimized" / "mlx" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "weights.py").write_text(
        "DIT_BUNDLES = {'medium': [('models/mlx/dit.npz', 'MLX/dit.npz'), ('models/mlx/dec.npz', 'MLX/dec.npz')]}\n"
        "SHARED = [('models/mlx/t5.npz', 'MLX/t5.npz')]\n")
    import sys
    sys.modules.pop("weights", None)
    try:
        assert weights.missing("sa3-mlx", "medium", up) == [
            "models/mlx/dit.npz", "models/mlx/dec.npz", "models/mlx/t5.npz"]
        models = up / "optimized" / "mlx" / "models" / "mlx"
        models.mkdir(parents=True)
        (models / "dit.npz").touch()
        try:
            (models / "dec.npz").symlink_to(tmp_path / "cleared-hf-cache-file")  # dangling link
        except OSError:  # Windows without Developer Mode: WinError 1314
            pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
        assert weights.missing("sa3-mlx", "medium", up) == ["models/mlx/dec.npz", "models/mlx/t5.npz"]
        assert weights.file_count("medium", up) == 3
    finally:
        sys.modules.pop("weights", None)
        sys.path.remove(str(up / "optimized" / "mlx" / "scripts"))


def test_expand_models():
    assert weights.expand([]) == ["medium"]
    assert weights.expand(["all"]) == list(weights.MODELS)
    assert weights.expand(["sm-sfx", "sm-sfx"]) == ["sm-sfx"]
    with pytest.raises(ValueError, match="unknown"):
        weights.expand(["large"])


def test_gated_help_for_package_installs(package_install):
    text = weights.gated_help(["stabilityai/stable-audio-3-medium"], None, "Run this again: x setup")
    assert "uvx --from huggingface_hub hf auth login" in text
    assert "https://huggingface.co/stabilityai/stable-audio-3-medium" in text
    assert "Do not ask\nfor the token in the chat" in text and "Run this again: x setup" in text


# ── the server's weights gate ────────────────────────────────────────────────
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_package_install_requires_setup_before_generating(fake, package_install):
    fake.missing_weights = lambda: ["models/mlx/dit_medium_f16.npz"]
    with pytest.raises(Exception, match=r"not downloaded yet.*" + re.escape(runtime.setup_command())):
        await server.generate_audio(prompt="x", duration_seconds=1)
    assert fake.requests == []  # nothing ran (no silent 7 GB download inside a tool call)

    fake.missing_weights = lambda: []
    out = await server.generate_audio(prompt="x", duration_seconds=1)
    assert len(out["candidates"]) == 1


@pytest.mark.anyio
async def test_checkout_keeps_download_on_first_use(fake, monkeypatch):
    monkeypatch.delenv("AUDIO_STUDIO_SA3_DIR", raising=False)
    fake.missing_weights = lambda: ["models/mlx/dit_medium_f16.npz"]
    out = await server.generate_audio(prompt="x", duration_seconds=1)
    assert len(out["candidates"]) == 1


def test_stdio_server_starts_before_setup(package_install, monkeypatch, capsys):
    monkeypatch.setattr(server, "resolve_backend_name", lambda: "sa3-mlx")
    ran = []
    monkeypatch.setattr(server.server, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(server, "create_backend", lambda: pytest.fail("must not load a backend"))
    server.main([])
    assert ran == [()]
    assert "not set up yet" in capsys.readouterr().err


def test_wrong_python_stops_with_the_fix(package_install, monkeypatch, capsys):
    def rosetta():
        raise RuntimeError("x86_64 build running under Rosetta ... cpython-3.11-macos-aarch64")
    monkeypatch.setattr(server, "resolve_backend_name", rosetta)
    with pytest.raises(SystemExit) as exc:
        server.main([])
    assert exc.value.code == 1 and "cpython-3.11-macos-aarch64" in capsys.readouterr().err


def test_web_mode_stops_with_the_setup_message(package_install, monkeypatch, capsys):
    def needs_setup():
        raise runtime.SetupRequired("weights missing; run setup")
    monkeypatch.setattr(server, "resolve_backend_name", lambda: "sa3-mlx")
    monkeypatch.setattr(runtime, "runtime_ready", lambda path=None: True)
    monkeypatch.setattr(server, "ready_backend", needs_setup)
    with pytest.raises(SystemExit) as exc:
        server.main(["--web", "8765", "--no-browser"])
    assert exc.value.code == 1 and "run setup" in capsys.readouterr().err


def test_setup_subcommand_and_version(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(firstrun, "main", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exc:
        server.main(["setup", "-y", "--model", "all"])
    assert exc.value.code == 0 and seen == [["-y", "--model", "all"]]
    with pytest.raises(SystemExit):
        server.main(["--version"])
    assert "audio-studio-mcp" in capsys.readouterr().out


# ── setup command ────────────────────────────────────────────────────────────
def test_setup_on_cpu_torch_with_nvidia_gpu_says_how_to_fix(monkeypatch, capsys):
    import types
    torch = types.SimpleNamespace(__version__="2.7.1+cpu", version=types.SimpleNamespace(cuda=None),
                                  cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(__import__("sys").modules, "torch", torch)
    monkeypatch.setattr(firstrun, "resolve_backend_name", lambda: "sa3-torch")
    monkeypatch.setattr(runtime, "nvidia_gpu_present", lambda: True)
    assert firstrun.main(["-y"]) == 1
    err = capsys.readouterr().err
    assert "CPU-only build" in err and "--torch-backend cu126" in err


def test_setup_downloads_after_showing_the_size(package_install, fake_download, monkeypatch, capsys):
    monkeypatch.setattr(firstrun, "resolve_backend_name", lambda: "sa3-mlx")
    monkeypatch.setattr(firstrun, "check_mlx", lambda: True)
    state = {"missing": ["a.npz"]}
    monkeypatch.setattr(weights, "missing", lambda b, n, up: list(state["missing"]))
    monkeypatch.setattr(weights, "file_count", lambda n, up: 1)
    monkeypatch.setattr(weights, "in_hf_cache", lambda n, up: False)
    calls = []

    def download(backend, names, upstream, retry):
        calls.append((backend, names, upstream))
        state["missing"] = []
        return 0
    monkeypatch.setattr(weights, "download", download)
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:  # Linux without the cuda extra (CI): setup only reads the cache path
        import sys
        import types
        fake_hf = types.ModuleType("huggingface_hub")
        fake_hf.constants = types.SimpleNamespace(HF_HUB_CACHE="hf-cache")
        monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    assert firstrun.main(["-y"]) == 0
    out = capsys.readouterr().out
    assert "Download size:" in out and "~6.9 GB" in out and "Stability AI Community License" in out
    assert calls == [("sa3-mlx", ["medium"], runtime.upstream_dir())]
    # The uvx line of the OS the tests run on (the backend is faked as sa3-mlx).
    assert f"claude mcp add --scope user audio-studio -- {runtime.run_command()}\n" in out

    assert firstrun.main([]) == 0  # second run: nothing to download
    assert "already downloaded" in capsys.readouterr().out and len(calls) == 1


def test_null_stdin_is_not_a_terminal():
    """Agents often start setup with stdin = NUL / /dev/null. On Windows isatty()
    is true for NUL, and setup used to ask "Download now?", read EOF and stop."""
    import subprocess
    import sys
    code = "from audio_studio_mcp import firstrun; print(firstrun.stdin_is_terminal())"
    out = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert out.strip() == "False"


def test_server_json_matches_the_package():
    meta = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {meta['name']}" in text
    assert len(meta["description"]) <= 100
    (pkg,) = meta["packages"]
    assert pkg["registryType"] == "pypi" and pkg["identifier"] == "audio-studio-mcp"
    import tomllib
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    from audio_studio_mcp import __version__
    assert meta["version"] == pkg["version"] == version == __version__
