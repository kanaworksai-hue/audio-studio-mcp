"""Run `audio-studio-mcp` with the fake backend (tests and UI screenshots).

    python tests/fake_web_server.py --web 8799 --no-browser
FAKE_DELAY=seconds makes each generation take that long (to see the queue)."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import FakeBackend  # noqa: E402

from audio_studio_mcp import server  # noqa: E402


class SlowFake(FakeBackend):
    def _generate(self, seeds, req):
        time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
        return super()._generate(seeds, req)


server.set_backend(SlowFake())
server.main(sys.argv[1:])
