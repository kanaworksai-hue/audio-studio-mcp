# Local songs with YuE2

YuE2 generates vocals and accompaniment from lyrics and a style description. The optional song backend uses audio.cpp with Metal on Apple Silicon and CUDA on Windows x64. The Songs tab and MCP share task IDs, status, cancellation and results. Stable Audio 3 remains the backend for instrumental BGM, sound effects and audio editing.

## Install when needed

Use the same Audio Studio environment and data directory you use for generation.

~~~sh
uv run audio-studio-mcp song-setup
uv run audio-studio-mcp song-setup --profile quality
~~~

On Apple Silicon, setup checks installed memory against the song policy before prompting for the license or looking for build tools. Setup then builds the pinned audio.cpp CLI using Git, CMake and Xcode with the Metal compiler installed. Set DEVELOPER_DIR to the full Xcode Developer directory when Xcode lives outside its standard location. Both song-setup --check and the Songs tab report when installed memory is below the policy minimum or the Metal runtime needs installation.

On Windows x64, setup installs the pinned audio.cpp v0.9.5 application and CUDA 13.3 redistributable ZIPs (0.86 GB of downloads, about 1.12 GB installed). Use an NVIDIA R610 or newer driver and a GPU with compute capability 7.5 or newer. CUDA DLLs are installed beside the CLI. The NVIDIA graphics driver supplies nvidia-smi and the system CUDA driver. A separate CUDA Toolkit install is unnecessary. For offline import or runtime repair, pass --runtime-from DIR with the two official Windows x64 CUDA 13.3 ZIPs. [Pinned Windows release](https://github.com/0xShug0/audio.cpp/releases/tag/v0.9.5) · [NVIDIA driver branches](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html#cuda-driver)

Use --accept-license for an unattended install after you have reviewed the terms, or --weights-from DIR to import a pinned GGUF snapshot. Setup verifies file sizes and SHA-256 before installing weights or unpacking Windows ZIPs. Local clients sharing AUDIO_STUDIO_HOME share the songs installation. Run **audio-studio-mcp song-setup --check** to verify installed files and see the probed device and Auto selection without downloading. The command lists whichever profiles are present, so missing optional weights can appear in the output when Auto is ready.

| Profile to install | Quantization | Total files |
| --- | --- | ---: |
| Auto | Q4_0 or Q8_0 selected for this machine, and F16 VAE | 2.93 or 4.53 GB |
| Fast | Q4_0 and F16 VAE | 2.93 GB |
| Highest quality | Q8_0 and F16 VAE | 4.53 GB |
| Both | Q4_0, Q8_0 and shared F16 VAE | 7.20 GB |

The weights are CC BY-NC 4.0. YuE2 offers additional rights to individual creators for generating and monetizing outputs. Its terms describe a separate licensing path for companies using the weights commercially. [YuE2 model terms](https://github.com/multimodal-art-projection/YuE/blob/1116bea76032bee5668e5f097294d082fa0b84f0/MODEL_LICENSE) · [Pinned GGUF snapshot](https://huggingface.co/audio-cpp/Yue2-3B-GGUF/tree/cca74d8980c00f30ab8d52248a367c01ad633ec2)

## Generate, play and export

Open Songs in the WebUI, or call generate_song with arguments like these.

~~~json
{
  "title": "The road at dawn",
  "style": "English vocals, acoustic indie pop, warm lead vocal, brushed drums",
  "lyrics": "[Verse]\nThe houses turn from blue to gold\nI fold your letter by the door\n\n[Chorus]\nI'll walk the road that leads me home\nAnd leave the porch light on once more",
  "duration_seconds": 90,
  "seed": 1234,
  "mode": "auto"
}
~~~

- **generate_song** returns a job_id and immediately releases the request. The duration_seconds cap defaults to 180 and accepts 8–240 seconds. The model can end earlier. For 30 seconds or less, use one short chorus and ask for the lead vocal on the first beat in the style prompt. Omitting seed generates a random seed that is saved with the result.
- **song_status** takes a job_id. It reports the queue position, synthesis phase, and, once selected, quantization, estimated seconds, and reasons for skipping another profile. The done response carries candidates[0].path, seed and metadata, and a replay_from path. end_reason is eos or duration_limit. New songs that reach the cap fade out over the last two seconds.
- **cancel_song** takes a job_id. Queued jobs cancel immediately. Running song jobs cancel after the subprocess exits and temporary files are removed.
- **Replay** calls generate_song with only replay_from set to the original WAV path in the output directory. It uses the saved seed, synthesis parameters, profile, revisions and duration-limit fade. Originals saved before the fade retain their original ending on replay. The WebUI exposes this on the song result and in the Library.
- **Playback and export** reuse the chat card with MCP Apps. The card polls the submitted job through its host and can cancel a running song. song_status itself is a text tool, so a model that also polls does not add more cards. A text client can poll song_status and use the returned file path. The WebUI reuses its waveform player and export form. The original WAV is stereo, 48 kHz and PCM16. Exports use the selected format and rate.

The JSON sidecar records lyrics, style, seed, full request, runtime/weights revisions, quantization, actual settings, start resources, wall time and WAV duration. A song job lives in the server that created it. The output WAV and its metadata remain available for replay after restart.

## Resource policy

Auto tries the highest precision installed profile that fits current RAM, GPU headroom, and the waiting-time estimate for the requested duration. Fast selects Q4_0. Highest quality selects Q8_0. Manual and replay modes still check resources and may exceed the Auto time budget. Auto can retry Q4 after a Q8 allocation failure if Q4 is installed and fits the refreshed resource checks and remaining time. Install --profile both to let Auto use either profile.

After each finished song, the server compares its synthesis time with the policy estimate for the audio length it actually generated. It stores the ratio in `songs/speed.json` under AUDIO_STUDIO_HOME by profile, device, runtime and length (up to 60 seconds, over 60 to under 150 seconds, or 150 seconds and above). Each ratio is limited to 0.25–8× and averaged with the previous value in the same group. The job estimate, Auto budget check and `song-setup --check` use the group for the requested length. When that group has no measurements, they use the policy estimate. Older entries without length information are ignored. Delete the file to reset calibration.

During synthesis the server watches memory. On macOS, critical memory pressure stops the song at once, and warning-level pressure stops it if it lasts 15 seconds; short warnings while the model loads are tolerated. If macOS kills audio.cpp (SIGKILL), the job reports a memory error and Auto can fall back from Q8 to Q4 like any other allocation failure. On Windows, the song stops when available RAM or commit headroom falls below 1 GiB.

For a long request that exceeds the default Auto budget, raise wait_budget_seconds or use the Auto time budget field in the WebUI's Advanced settings.

### macOS Metal

| Setting | Q4_0 | Q8_0 |
| --- | ---: | ---: |
| Total unified RAM | 32 GiB | 32 GiB |
| Estimated system RAM available at start | 10 GiB | 14.5 GiB |
| Metal working-set headroom | 5 GiB | 7 GiB |
| Estimated wall time for a 180 s request | 231 s | 283 s |

The starting values come from the 32 GiB Apple M6 measurements. On that machine the default 240-second budget selects Q4 for 180-second requests. A strict budget of 180 seconds excludes both profiles for that requested length. Actual time, lyric coverage and ending vary with content, hardware and system load. Lower-RAM Macs have not been qualified. The Metal number is the probe process's working-set headroom. System-wide contention is checked through macOS memory availability and pressure.

### Windows CUDA

| Setting | Q4_0 | Q8_0 |
| --- | ---: | ---: |
| System RAM detected by Windows | 31 GiB | 31 GiB |
| Available RAM and commit headroom at start | 8 GiB | 10 GiB |
| Free dedicated VRAM at start | 6 GiB | 8.2 GiB |
| Estimated wall time for a 180 s request | 164 s | 177 s |

The default Windows budget is 180 seconds. Profile thresholds are based on the tested RTX 3060 12 GiB system. Windows may expose slightly less physical memory than installed. Auto takes the lower of the CUDA and NVIDIA driver free-memory readings and checks again after unloading local SA3. When Q8 is ineligible, Auto considers Q4 if installed. Free VRAM can change as other GPU apps run. These checks describe runtime headroom. Support on smaller VRAM or RAM configurations still needs separate real hardware validation.

### Overrides

Set wait_budget_seconds per Auto request or set AUDIO_STUDIO_SONG_POLICY to an absolute path to a local JSON file. Omitted top-level fields take their defaults. When supplying q4 or q8, include all four tuning fields. The following example uses the macOS defaults.

~~~json
{
  "min_total_gib": 32,
  "wait_budget_seconds": 240,
  "q4": {
    "min_available_gib": 10,
    "min_gpu_gib": 5,
    "setup_seconds": 20,
    "seconds_per_second": 1.17
  },
  "q8": {
    "min_available_gib": 14.5,
    "min_gpu_gib": 7,
    "setup_seconds": 22,
    "seconds_per_second": 1.45
  }
}
~~~

Stable Audio 3 generation, preloading and unload share the same GPU lease as YuE2. The lease also serializes work across new Audio Studio processes using the same AUDIO_STUDIO_HOME. Before a song starts, the server evicts its resident SA3 models on the SA3 worker and checks current resources again. Other processes' resident caches count against available RAM. Job output is staged alongside the destination and published after format validation.

## Backend integration

songs.py contains requests, profile selection and the shared audio.cpp runner. song_cuda.py probes Windows RAM, NVIDIA drivers and the visible CUDA devices. song_setup.py installs the pinned runtime and weights. web.JobQueue owns the WebUI and MCP song lifecycle. server.run_song_job owns the GPU lease, SA3 eviction, cancellation and output publication.

[Metal runtime source](https://github.com/0xShug0/audio.cpp/blob/cf4929424ac27e487d09fb64702c72681cc18ba8/docs/models/yue2.md) · [CUDA runtime source](https://github.com/0xShug0/audio.cpp/blob/61c7838baf1f7f34769b5b7bd369c0d78edf9535/docs/models/yue2.md)
