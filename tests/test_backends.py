"""resolve_backend_name(): platform detection, including x86_64 Python under Rosetta."""
import subprocess

import pytest

from audio_studio_mcp import backends


@pytest.fixture
def mac(monkeypatch):
    """Pretend to run on macOS with the given machine and sysctl.proc_translated output."""
    monkeypatch.delenv("AUDIO_STUDIO_BACKEND", raising=False)
    calls = []

    def setup(machine, translated=None, system="Darwin"):
        monkeypatch.setattr(backends.platform, "system", lambda: system)
        monkeypatch.setattr(backends.platform, "machine", lambda: machine)

        def run(cmd, **kwargs):
            calls.append(cmd)
            if translated is None:  # Intel Macs have no sysctl.proc_translated
                return subprocess.CompletedProcess(cmd, 1, "", "unknown oid 'sysctl.proc_translated'")
            return subprocess.CompletedProcess(cmd, 0, f"{translated}\n", "")

        monkeypatch.setattr(backends.subprocess, "run", run)
        return calls

    return setup


def test_apple_silicon_native_is_mlx(mac):
    calls = mac("arm64")
    assert backends.resolve_backend_name() == "sa3-mlx"
    assert calls == []


def test_x86_python_under_rosetta(mac):
    calls = mac("x86_64", translated=1)
    with pytest.raises(RuntimeError, match="Rosetta") as exc:
        backends.resolve_backend_name()
    assert "Intel Macs" not in str(exc.value)
    assert "cpython-3.11-macos-aarch64" in str(exc.value)
    assert calls == [["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"]]


def test_real_intel_mac(mac):
    mac("x86_64", translated=None)
    with pytest.raises(RuntimeError, match="Intel Macs are not supported"):
        backends.resolve_backend_name()


def test_native_x86_process_on_intel_mac(mac):
    mac("x86_64", translated=0)
    with pytest.raises(RuntimeError, match="Intel Macs are not supported"):
        backends.resolve_backend_name()


def test_sysctl_missing(mac, monkeypatch):
    mac("x86_64")

    def run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(backends.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Intel Macs are not supported"):
        backends.resolve_backend_name()


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_other_platforms_are_torch(mac, system):
    calls = mac("AMD64", system=system)
    assert backends.resolve_backend_name() == "sa3-torch"
    assert calls == []


def test_explicit_backend_skips_detection(mac):
    calls = mac("x86_64", translated=1)
    assert backends.resolve_backend_name("sa3-torch") == "sa3-torch"
    assert calls == []


def test_unknown_backend(mac):
    mac("arm64")
    with pytest.raises(RuntimeError, match="unknown AUDIO_STUDIO_BACKEND"):
        backends.resolve_backend_name("cuda")
