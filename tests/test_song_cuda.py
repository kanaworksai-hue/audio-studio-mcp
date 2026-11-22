"""Regression coverage for the Windows song runtime and release installer."""
from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile

import pytest

from audio_studio_mcp import song_cuda, song_setup, songs


def test_cuda_auto_uses_live_vram_and_keeps_manual_quality_strict():
    cfg = songs.CudaPolicy()
    request = songs.SongRequest(seed=1234)
    installed = {"cuda-q4", "cuda-q8"}

    def choose(vram, ram=32, req=request, **kwargs):
        resources = songs.Resources(47.9, ram, vram, 1, "RTX 3060")
        return songs.select(cfg, resources, installed, req, backend="cuda", **kwargs)

    assert choose(10).profile.id == "cuda-q8"
    assert choose(8.2).estimate_seconds == 177
    fallback = choose(6.5)
    assert fallback.profile.id == "cuda-q4"
    assert "8.2 GiB free NVIDIA VRAM" in fallback.reasons[0]
    assert choose(10, ram=9).profile.id == "cuda-q4"
    assert choose(10, excluded={"cuda-q8"}).reasons == ("Q8_0 ran out of memory.",)
    with pytest.raises(songs.SongRuntimeError, match="free NVIDIA VRAM"):
        choose(5.9)
    with pytest.raises(songs.SongRuntimeError, match="available RAM"):
        choose(10, ram=7.9)
    with pytest.raises(songs.SongRuntimeError, match="free NVIDIA VRAM"):
        choose(6.5, req=request.model_copy(update={"mode": "quality"}))
    with pytest.raises(songs.SongRuntimeError, match="free NVIDIA VRAM"):
        choose(6.5, pinned="cuda-q8")
    long = songs.SongRequest(seed=1234, duration_seconds=240)
    with pytest.raises(songs.SongRuntimeError, match="220s exceeds the 180s Auto budget"):
        choose(10, req=long)
    assert choose(10, req=long.model_copy(update={"wait_budget_seconds": 240})).profile.id == "cuda-q8"


def test_cuda_driver_preflight_reports_old_driver_and_unsupported_gpu():
    with pytest.raises(RuntimeError, match=r"R610.*595.45"):
        song_cuda._check_driver(song_cuda.NvidiaDevice("595.45", (8, 6), 10 << 30))
    with pytest.raises(RuntimeError, match=r"compute capability 7.5.*6.1"):
        song_cuda._check_driver(song_cuda.NvidiaDevice("610.88", (6, 1), 10 << 30))


def test_cuda_probe_matches_pci_and_uses_lower_free_vram(tmp_path, monkeypatch):
    binary = tmp_path / "音 楽" / "audiocpp_cli.exe"
    binary.parent.mkdir()
    binary.touch()
    for name in song_cuda.REQUIRED_DLLS:
        (binary.parent / name).touch()
    monkeypatch.setattr(song_cuda, "nvidia_devices", lambda: {
        (0, 9, 0, 0): song_cuda.NvidiaDevice("610.88", (8, 6), 7 << 30),
        (0, 10, 0, 0): song_cuda.NvidiaDevice("610.88", (8, 6), 11 << 30),
    })
    monkeypatch.setattr(song_cuda, "process_options", dict)
    devices = [
        {"backend": "cpu", "pci_bus_id": None},
        {"backend": "cuda", "pci_bus_id": "0000:09:00.0", "description": "CUDA card",
         "index": 0, "memory": {"free_bytes": 10 << 30, "total_bytes": 12 << 30}},
        {"backend": "cuda", "pci_bus_id": "0000:0a:00.0", "description": "Other CUDA card",
         "index": 1, "memory": {"free_bytes": 6 << 30, "total_bytes": 12 << 30}},
    ]
    result = subprocess.CompletedProcess([], 0, json.dumps({"devices": devices}), "")
    monkeypatch.setattr(song_cuda.subprocess, "run", lambda *args, **kwargs: result)
    gpu = song_cuda.probe(binary)
    assert (gpu.index, gpu.free_bytes, gpu.compute) == (0, 7 << 30, "8.6")

    result.returncode = 0xC0000135
    with pytest.raises(RuntimeError, match="DLL could not be loaded"):
        song_cuda.probe(binary)
    (binary.parent / "cufft64_12.dll").unlink()
    with pytest.raises(RuntimeError, match="Missing app-local.*cufft64_12.dll"):
        song_cuda.probe(binary)


def test_cuda_installer_verifies_archives_before_publishing(tmp_path, monkeypatch):
    archives = tmp_path / "archives"
    archives.mkdir()
    binary = tmp_path / "songs" / "cuda" / "audiocpp_cli.exe"
    packages = {}
    for filename, member in (("bin.zip", binary.name), ("cudart.zip", "cudart64_13.dll")):
        archive = archives / filename
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(member, member)
        data = archive.read_bytes()
        packages[filename] = len(data), hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(song_cuda, "ASSETS", packages)

    def probe(staged):
        assert staged.is_file() and (staged.parent / "cudart64_13.dll").is_file()
        assert not binary.exists()

    monkeypatch.setattr(songs, "probe_cuda", probe)
    bad = archives / "cudart.zip"
    valid = bad.read_bytes()
    bad.write_bytes(bytes(len(valid)))
    with pytest.raises(RuntimeError, match="SHA-256 mismatch.*cudart.zip"):
        song_setup._install_cuda_runtime(binary, archives)
    assert not binary.parent.exists()
    bad.write_bytes(valid)
    song_setup._install_cuda_runtime(binary, archives)
    assert binary.is_file()
    assert (binary.parent / "cudart64_13.dll").is_file()
