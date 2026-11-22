"""Windows discovery for the pinned audio.cpp CUDA subprocess."""
from __future__ import annotations

import csv
import ctypes
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TAG = "v0.9.5"
REVISION = "61c7838baf1f7f34769b5b7bd369c0d78edf9535"
RELEASE_URL = f"https://github.com/0xShug0/audio.cpp/releases/download/{TAG}"
ASSETS = {
    f"audio-{TAG}-bin-windows-x64-cuda13.3.zip":
        (280869815, "9d413d8565ad26e1ae33743e9c884e733f1684169be6f8934e1af9ee2e57a872"),
    f"audio-{TAG}-cudart-windows-x64-cuda13.3.zip":
        (575457454, "8614040110cc5852b1dd2192e80344295500f4fa7f3199a51c737c2c7549c3dc"),
}
REQUIRED_DLLS = (
    "ggml.dll", "ggml-base.dll", "ggml-cuda.dll", "cudart64_13.dll",
    "cublas64_13.dll", "cublasLt64_13.dll", "cufft64_12.dll",
)
MIN_DRIVER_BRANCH = 610
MIN_COMPUTE = (7, 5)


@dataclass(frozen=True)
class NvidiaDevice:
    driver: str
    compute: tuple[int, ...]
    free_bytes: int


@dataclass(frozen=True)
class CudaDevice:
    name: str
    index: int
    free_bytes: int
    total_bytes: int
    driver: str
    compute: str


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("physical_total", ctypes.c_uint64),
        ("physical_available", ctypes.c_uint64),
        ("commit_total", ctypes.c_uint64),
        ("commit_available", ctypes.c_uint64),
        ("virtual_total", ctypes.c_uint64),
        ("virtual_available", ctypes.c_uint64),
        ("extended_available", ctypes.c_uint64),
    ]


def memory_status() -> tuple[int, int]:
    status = _MemoryStatus()
    status.length = ctypes.sizeof(status)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return status.physical_total, min(status.physical_available, status.commit_available)


def process_options() -> dict:
    # DLLs for this build live beside the executable. Isolate them from other
    # CUDA installations and keep native commands from opening console windows.
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return {
        "env": dict(os.environ, PATH=os.pathsep.join([str(windows / "System32"), str(windows)])),
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _pci_key(bus_id: str) -> tuple[int, int, int, int]:
    domain, bus, device, function = bus_id.replace(".", ":").split(":")
    return tuple(int(part, 16) for part in (domain, bus, device, function))


def _check_driver(device: NvidiaDevice) -> None:
    if int(device.driver.split(".")[0]) < MIN_DRIVER_BRANCH:
        raise RuntimeError(f"CUDA 13.3 song generation requires an NVIDIA R{MIN_DRIVER_BRANCH} "
                           f"or newer driver. Installed driver: {device.driver}. Update the NVIDIA driver.")
    if device.compute < MIN_COMPUTE:
        capability = ".".join(map(str, device.compute))
        raise RuntimeError(f"The CUDA 13.3 runtime needs compute capability 7.5 or newer "
                           f"(detected {capability}).")


def nvidia_devices() -> dict[tuple[int, int, int, int], NvidiaDevice]:
    system_smi = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
    command = system_smi if system_smi.is_file() else shutil.which("nvidia-smi.exe")
    if command is None:
        raise RuntimeError("NVIDIA driver diagnostics (nvidia-smi.exe) are unavailable. "
                           "Install or repair the NVIDIA graphics driver.")
    try:
        result = subprocess.run([
            str(command), "--query-gpu=pci.bus_id,driver_version,compute_cap,memory.free",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            **process_options())
        if result.returncode or not result.stdout.strip():
            detail = _last_line(result.stderr or result.stdout)
            raise RuntimeError(f"NVIDIA driver could not report a GPU. {detail} "
                               "Connect a supported NVIDIA GPU or update its driver.")
        devices = {
            _pci_key(bus.strip()): NvidiaDevice(driver.strip(), tuple(map(int, compute.strip().split("."))),
                                                int(free.strip()) * (1 << 20))
            for bus, driver, compute, free in csv.reader(result.stdout.splitlines())
        }
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError("Could not read NVIDIA driver, compute capability, and free VRAM "
                           "with nvidia-smi. Check the NVIDIA driver installation.") from error
    supported = {}
    reasons = []
    for bus_id, device in devices.items():
        try:
            _check_driver(device)
        except RuntimeError as error:
            reasons.append(str(error))
        else:
            supported[bus_id] = device
    if not supported:
        raise RuntimeError(" ".join(reasons) or "No supported NVIDIA GPU was found.")
    return supported


def _last_line(text: str) -> str:
    return next((line.strip()[:300] for line in reversed(text.splitlines()) if line.strip()), "")


def runtime_problem(binary: Path) -> str | None:
    if not binary.is_file():
        return f"Missing {binary.name}."
    missing = [name for name in REQUIRED_DLLS if not (binary.parent / name).is_file()]
    if missing:
        return f"Missing app-local CUDA runtime files: {', '.join(missing)}."
    return None


def launch_problem(code: int) -> str:
    return {
        0xC0000135: "A Windows runtime DLL could not be loaded. Repair song-setup; check the Visual C++ x64 runtime.",
        0xC000007B: "Windows rejected a runtime DLL with the wrong architecture. Repair song-setup.",
        0xC0000139: "Windows could not resolve a DLL entry point. Repair song-setup and update the NVIDIA driver.",
        0xC0000142: "Windows could not initialize a runtime DLL. Repair song-setup and update the NVIDIA driver.",
    }.get(code & 0xFFFFFFFF, "")


def probe(binary: Path) -> CudaDevice:
    devices = nvidia_devices()
    if problem := runtime_problem(binary):
        raise RuntimeError(problem)
    try:
        result = subprocess.run([str(binary), "--list-devices", "--json"], cwd=binary.parent,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=20,
                                **process_options())
        if result.returncode:
            detail = launch_problem(result.returncode) or _last_line(result.stderr)
            raise RuntimeError(f"CUDA runtime probe failed (exit {result.returncode:#x}). {detail}")
        candidates = []
        for item in json.loads(result.stdout)["devices"]:
            if item["backend"] != "cuda" or not item["pci_bus_id"]:
                continue
            hardware = devices.get(_pci_key(item["pci_bus_id"]))
            if hardware is None:
                continue
            memory = item["memory"]
            # WDDM CUDA and NVML availability can differ. Use the lower reading.
            candidates.append(CudaDevice(
                item["description"].strip(), int(item["index"]),
                min(int(memory["free_bytes"]), hardware.free_bytes), int(memory["total_bytes"]),
                hardware.driver, ".".join(map(str, hardware.compute)),
            ))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        raise RuntimeError("Could not read devices from the pinned CUDA runtime. "
                           "Check the NVIDIA driver and run song-setup to repair the runtime.") from error
    if not candidates:
        detail = _last_line(result.stderr)
        raise RuntimeError("audio.cpp found no usable CUDA device. Check CUDA_VISIBLE_DEVICES, "
                           f"the NVIDIA driver and app-local CUDA DLLs. {detail}")
    return max(candidates, key=lambda device: device.free_bytes)
