"""Backend interface: one model runtime (e.g. Stable Audio 3 on MLX).

A backend owns a single worker thread. Every model call (load, generate,
unload) runs on it, so generations are serialized and runtimes that bind GPU
streams to a thread (MLX) stay on the thread that created them.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Executor
from dataclasses import dataclass, field

import numpy as np


@dataclass
class GenerationRequest:
    """Backend-neutral generation parameters.

    init_audio + init_strength = audio-to-audio variation;
    inpaint_audio + inpaint_range = regenerate [start, end) seconds, keep the rest
    (also used for continuation by inpainting past the end of the input)."""
    prompt: str
    seconds: float
    negative_prompt: str = ""
    steps: int = 8
    cfg: float = 1.0
    init_audio: str | None = None
    init_strength: float = 1.0
    inpaint_audio: str | None = None
    inpaint_range: tuple[float, float] | None = None


@dataclass
class GenerationResult:
    audios: list[np.ndarray] = field(default_factory=list)  # (channels, samples) float32
    wall_seconds: float = 0.0
    included_model_load: bool = False


class Backend(ABC):
    name: str = ""
    model_label: str = ""   # e.g. "medium + same-l", stored in metadata
    sample_rate: int = 44100
    max_seconds: float = 380.0

    def __init__(self) -> None:
        self._last_used = time.time()

    # ── implemented by each backend ──────────────────────────────────────────
    @property
    @abstractmethod
    def executor(self) -> Executor:
        """Single-worker executor all model calls run on."""

    @abstractmethod
    def loaded(self) -> bool: ...

    @abstractmethod
    def _generate(self, seeds: list[int], req: GenerationRequest) -> GenerationResult:
        """Blocking; runs on the executor thread. One audio per seed."""

    @abstractmethod
    def _unload(self) -> bool:
        """Blocking; free model memory. Returns whether anything was loaded."""

    @abstractmethod
    def _preload(self) -> None:
        """Blocking; load the models needed for text-to-audio."""

    # ── shared behaviour ─────────────────────────────────────────────────────
    async def generate(self, seeds: list[int], req: GenerationRequest) -> GenerationResult:
        def job():
            try:
                return self._generate(seeds, req)
            finally:
                # Stamped on the executor thread, so an idle check queued behind
                # a long generation sees it before it runs.
                self._last_used = time.time()

        self._last_used = time.time()
        return await asyncio.wrap_future(self.executor.submit(job))

    async def unload(self) -> bool:
        return await asyncio.wrap_future(self.executor.submit(self._unload))

    def preload(self) -> None:
        def report(fut):
            if fut.exception() is not None:
                print(f"[audio-studio] preload failed: {fut.exception()!r}", file=sys.stderr)
            else:
                print("[audio-studio] models preloaded", file=sys.stderr)

        self._last_used = time.time()
        self.executor.submit(self._preload).add_done_callback(report)

    def start_idle_reaper(self, minutes: float) -> None:
        """Free the models after `minutes` without a request (0 = never)."""
        if minutes <= 0:
            return

        def unload_if_idle():
            # Runs on the executor, so it is serialized with generations.
            if self.loaded() and time.time() - self._last_used > minutes * 60:
                self._unload()
                print(f"[audio-studio] idle {minutes:g} min → models unloaded", file=sys.stderr)

        def loop():
            while True:
                time.sleep(30)
                if self.loaded():
                    self.executor.submit(unload_if_idle)

        threading.Thread(target=loop, daemon=True, name="idle-reaper").start()
