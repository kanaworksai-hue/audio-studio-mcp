"""Prompt preparation for Stable Audio 3 (backend-independent).

The TrackType prefixes and the 'Length: N seconds' convention come from the
official prompt assistant (stable_audio_3/interface/reprompt.py upstream).
"""
from __future__ import annotations

import re

TRACK_TYPE_PREFIXES = {
    "music": "TrackType: Music, VocalType: Instrumental, ",
    "instrument": "TrackType: Instrument, ",
    "sfx": "TrackType: SFX, ",
    "raw": "",
}
DEFAULT_SECONDS = {"music": 60.0, "instrument": 12.0, "sfx": 5.0, "raw": 30.0}

_LENGTH_RE = re.compile(r"\s*\.?\s*Length:\s*(\d+(?:\.\d+)?)\s*seconds?\.?\s*$", re.I)
_BPM_RE = re.compile(r"\bBPM\b", re.I)
_PREFIX_RE = re.compile(r"^TrackType:[^,]*,\s*(VocalType:[^,]*,\s*)?")


def prepare_prompt(prompt: str, audio_type: str, seconds: float | None,
                   bpm: int | None = None, sfx_fields: tuple[str, ...] = ()
                   ) -> tuple[str, float | None]:
    """Build the prompt the model sees:
      prompt [, source, action, material, space].  [BPM: N.]
    with the official TrackType prefix, and pull a trailing 'Length: N seconds'
    (the prompt assistant's format) out of the text into the duration."""
    text = prompt.strip()
    m = _LENGTH_RE.search(text)
    if m:
        text = text[:m.start()]
        if seconds is None:
            seconds = float(m.group(1))
    parts = [p.strip().rstrip(" .,") for p in (text, *sfx_fields) if p and p.strip()]
    if not parts:
        raise ValueError("empty prompt: give prompt and/or the sound_* fields")
    text = ", ".join(parts) + "."
    if bpm and not _BPM_RE.search(text):
        text += f" BPM: {bpm}."
    if audio_type != "raw" and not text.lower().startswith("tracktype:"):
        text = TRACK_TYPE_PREFIXES[audio_type] + text
    return text, seconds


def strip_prefix(prompt: str) -> str:
    """Drop the TrackType/VocalType prefix (used for auto file names)."""
    return _PREFIX_RE.sub("", prompt)


INSTRUCTIONS = """\
Local Stable Audio 3 running on this machine, for BGM and sound effects.
Instrumental only — it cannot produce intelligible vocals/lyrics.
generate_audio/edit_audio write 44.1 kHz 24-bit stereo WAV + a .json sidecar
(prompt, seed, settings) next to each file; process_audio trims, loops, fades,
normalizes and exports 48 kHz WAV/OGG/FLAC for video or game engines.

Prompting (official SA3 prompt guide + prompt-assistant rules):
- Pick audio_type; the server adds the official prefix the model was trained on:
  music → "TrackType: Music, VocalType: Instrumental, ", instrument (solo/stem/loop)
  → "TrackType: Instrument, ", sfx (sound effects, ambience, one-shots) →
  "TrackType: SFX, ". Without the SFX prefix, effects often come out as music.
- Write English, one or two dense natural sentences, ≤ ~45 words, no markdown.
  Translate other languages to English. Avoid vocal words (vocals, singer,
  lyrics, rap, choir voice…). Put the user's own wording in original_idea.
- Music: genre/style + adjectives → main instruments → supporting layers/FX →
  drums/rhythm → mood/energy woven into the sentence. Pass tempo as bpm.
  e.g. "Lo-fi hip-hop chill track with mellow electric piano, soft vinyl crackle,
  low-pass filtered drums and soft plucked bass for a relaxed, dreamy vibe."
- Instrument/stem/loop: instrument + technique + timbre/material + style/mood +
  recording/production qualities, plus bpm. Loops 6–20 s, stems 20–180 s.
- SFX: fill sound_source (what makes it), sound_action (how it is triggered,
  how it evolves: attack, decay, movement/doppler), sound_material (texture,
  timbre, material) and sound_space (room, distance, dry/reverberant, mic);
  prompt can hold an overall one-line description. They are joined in that order.
  e.g. source "glass bottle", action "shattering on impact, fragments scattering",
  material "thick green glass, sharp bright transient", space "concrete floor,
  close-up, dry".
- Duration should fit the content: impacts/clicks 1–3 s, actions 3–6 s,
  ambience 6–15 s (longer is fine), one-shots 1–3 s, loops 6–20 s,
  pop/rock 180–210 s, dance 120–180 s, cinematic/ambient 240–300 s. Max 380 s
  with the default medium model, 120 s with the small models.
- Precise timing instructions ("climax at 15 s") are not followed reliably;
  for structure, generate then use edit_audio inpaint/continue.
- Defaults steps=8, cfg=1.0 are the official distilled settings; more steps do
  not help. For stronger prompt adherence or to use negative_prompt, raise cfg
  (try 3–7, ≈2× slower). Results vary a lot per seed: use variations=2–4 and
  pick; candidates use seeds seed, seed+1, … so any one is reproducible.
- Loudness is not mastered: generated files only get a safety ceiling (peak
  above −1 dBFS is turned down, never up). Use process_audio to normalize.
"""
