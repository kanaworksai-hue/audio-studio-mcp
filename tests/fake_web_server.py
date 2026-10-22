"""Run `audio-studio-mcp` with the fake backend (tests and UI screenshots).

    python tests/fake_web_server.py --web 8799 --no-browser
FAKE_DELAY=seconds makes each generation take that long (to see the queue)."""
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from conftest import FakeBackend  # noqa: E402

from audio_studio_mcp import server  # noqa: E402


class SlowFake(FakeBackend):
    def _generate(self, seeds, req):
        time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
        res = super()._generate(seeds, req)
        # beats with decays, so the waveform looks like audio in screenshots
        for s, a in zip(seeds, res.audios):
            t = np.arange(a.shape[-1]) / self.sample_rate
            beat = 0.5 + s % 3 * 0.1
            env = np.exp(-6 * (t % beat)) * (0.35 + 0.3 * np.sin(2 * np.pi * t / (3 + s % 4)) ** 2)
            a *= env.astype(np.float32)
        return res


server.set_backend(SlowFake())
server.main(sys.argv[1:])
