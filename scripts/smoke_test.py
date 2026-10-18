"""End-to-end test against the real model, over stdio like an MCP client.

Usage: .venv/bin/python scripts/smoke_test.py [--out DIR]

Runs every tool once (generate, edit: variation/inpaint/continue, process,
unload). Writes into a temporary folder unless --out is given. Takes about
30 seconds on an M1 once the weights are downloaded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters


async def call(client: Client, name: str, args: dict) -> dict:
    r = await client.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in r.content)
    print(f"\n{name} {json.dumps(args, ensure_ascii=False)}\n  → {text}")
    if r.is_error:
        sys.exit(f"FAILED: {name}")
    return json.loads(text)


async def run(out_dir: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "audio_studio_mcp"],
        env={**os.environ, "AUDIO_STUDIO_OUTPUT_DIR": str(out_dir)})
    async with Client(params, read_timeout_seconds=900) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        print("tools:", tools)
        assert tools == ["edit_audio", "generate_audio", "process_audio", "unload_models"], tools

        sfx = await call(c, "generate_audio", {
            "audio_type": "sfx", "duration_seconds": 3, "variations": 2, "seed": 42,
            "sound_source": "old wooden door", "sound_action": "creaking open slowly, then a soft thud",
            "sound_material": "dry aged oak, rusty iron hinges", "sound_space": "echoing empty hallway, close mic",
            "title": "smoke_door", "original_idea": "an old wooden door creaking open"})
        assert len(sfx["candidates"]) == 2 and sfx["prompt_used"].startswith("TrackType: SFX")

        music = await call(c, "generate_audio", {
            "prompt": "Synthwave track with warm analog pads, punchy gated drums and a shimmering "
                      "arpeggiated lead, nostalgic and driving",
            "bpm": 100, "duration_seconds": 20, "seed": 42, "title": "smoke_synthwave"})
        assert music["prompt_used"].endswith("BPM: 100.")
        mpath = music["candidates"][0]["path"]

        ogg = await call(c, "process_audio", {
            "input_audio": sfx["candidates"][0]["path"], "trim_silence": True, "mono": True,
            "normalize": "peak", "peak_ceiling_db": -1, "format": "ogg", "sample_rate": 48000})
        assert ogg["channels"] == 1 and ogg["sample_rate"] == 48000 and Path(ogg["path"]).is_file()

        loop = await call(c, "process_audio", {
            "input_audio": mpath, "normalize": "loudness", "target_lufs": -16,
            "loop": True, "loop_crossfade_ms": 200, "format": "wav", "sample_rate": 48000})
        assert abs(loop["output_lufs"] - -16) < 1.5, loop

        ext = await call(c, "edit_audio", {
            "input_audio": mpath, "mode": "continue", "duration_seconds": 30,
            "prompt": "Synthwave outro with fading pads", "seed": 1, "title": "smoke_synthwave_ext"})
        assert ext["seconds"] == 30

        await call(c, "edit_audio", {
            "input_audio": mpath, "mode": "inpaint", "start_seconds": 8, "end_seconds": 14,
            "prompt": "Synthwave breakdown with only pads and arpeggio, no drums", "seed": 2})
        await call(c, "edit_audio", {
            "input_audio": mpath, "mode": "variation", "strength": 0.5,
            "prompt": "Lo-fi hip-hop version with dusty drums and mellow electric piano", "seed": 3})

        assert (await call(c, "unload_models", {}))["unloaded"] is True

    files = sorted(p.name for p in out_dir.iterdir())
    print(f"\nOK — {len(files)} files in {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, help="keep outputs in this folder")
    args = ap.parse_args()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        asyncio.run(run(args.out.resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix="audio-studio-smoke-") as td:
            asyncio.run(run(Path(td)))


if __name__ == "__main__":
    main()
