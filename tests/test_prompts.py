import pytest

from audio_studio_mcp.prompts import prepare_prompt, strip_prefix


def test_music_prefix_and_bpm():
    text, seconds = prepare_prompt("Lo-fi hip-hop with mellow piano", "music", None, bpm=85)
    assert text == "TrackType: Music, VocalType: Instrumental, Lo-fi hip-hop with mellow piano. BPM: 85."
    assert seconds is None


def test_sfx_fields_are_joined_in_order():
    text, _ = prepare_prompt("", "sfx", 3, sfx_fields=("glass bottle", "shattering.", "", "concrete floor, dry"))
    assert text == "TrackType: SFX, glass bottle, shattering, concrete floor, dry."


def test_length_suffix_becomes_duration():
    text, seconds = prepare_prompt("Ambient drone. Length: 42 seconds.", "music", None)
    assert seconds == 42.0
    assert "Length" not in text


def test_explicit_duration_wins_over_length_suffix():
    _, seconds = prepare_prompt("Ambient drone. Length: 42 seconds", "music", 10)
    assert seconds == 10


def test_existing_prefix_and_bpm_are_kept():
    text, _ = prepare_prompt("TrackType: Instrument, drum loop, BPM: 120", "music", None, bpm=90)
    assert text == "TrackType: Instrument, drum loop, BPM: 120."


def test_raw_has_no_prefix():
    assert prepare_prompt("anything", "raw", None)[0] == "anything."


def test_empty_prompt_rejected():
    with pytest.raises(ValueError):
        prepare_prompt("  ", "sfx", None, sfx_fields=("", ""))


def test_strip_prefix():
    assert strip_prefix("TrackType: Music, VocalType: Instrumental, Jazz trio.") == "Jazz trio."
    assert strip_prefix("TrackType: SFX, door slam.") == "door slam."
