"""Real stdio subprocess tests (fake backend, no model)."""
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio

TESTS = Path(__file__).parent

# A backend whose preload swaps sys.stdout like the MLX backend does while it
# loads. Started before the transport claims stdout, it used to make every
# JSON-RPC reply go to stderr and the client hang on initialize.
SERVER = """
import contextlib, sys, time
from conftest import FakeBackend
from audio_studio_mcp import server

class SlowPreload(FakeBackend):
    def _preload(self):
        with contextlib.redirect_stdout(sys.stderr):
            print("loading weights...")
            time.sleep(2)
        self._loaded = True

server.set_backend(SlowPreload())
server.main(sys.argv[1:])
"""


async def test_preload_does_not_break_stdio(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-c", SERVER, "--preload"],
        env={**os.environ, "PYTHONPATH": str(TESTS), "AUDIO_STUDIO_OUTPUT_DIR": str(tmp_path)})
    with anyio.fail_after(30):
        async with Client(params, read_timeout_seconds=20) as c:
            await anyio.sleep(3)  # preload finished
            r = await c.call_tool("generate_audio", {"prompt": "x", "duration_seconds": 1, "seed": 1})
            assert not r.is_error, r.content
            assert '"included_model_load": false' in r.content[0].text
