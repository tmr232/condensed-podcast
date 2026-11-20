import asyncio
import hashlib
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from condensed_podcast.runtime import convert


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
@pytest.mark.parametrize(
    "settings, input_codec, bounds, extension",
    [
        ({}, "aac", (3.5, 4.6), ".ogg"),
        (
            {"padding": 0.1, "condensed_audio": {"audio_codec": "copy"}},
            "libopus",
            (2.1, 2.8),
            ".mkv",
        ),
    ],
)
def test_real_shuku_publish_and_skip_bad_input(
    library,
    config,
    settings,
    input_codec,
    bounds,
    extension,
):
    config = replace(config, shuku=config.shuku | settings)
    library.config = config
    source = config.sources[0]
    subtitles = source / "subtitles.srt"
    subtitles.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
        "2\n00:00:06,000 --> 00:00:07,000\nこんばんは\n",
        encoding="utf-8",
    )
    video = source / "Show S01E04 - 10 bit.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=5:duration=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-i",
            str(subtitles),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-c:v",
            "mpeg4",
            "-c:a",
            input_codec,
            "-c:s",
            "srt",
            "-metadata:s:a:0",
            "language=jpn",
            "-metadata:s:s:0",
            "language=eng",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    subtitles.unlink()  # Exercise embedded English fallback, not fuzzy external lookup.
    old = time.time() - 60
    os.utime(video, (old, old))
    checksum = hashlib.sha256(video.read_bytes()).hexdigest()
    library.scan()
    row = library.episodes()[0]
    asyncio.run(convert(library, row["id"]))
    row = library.episode(row["id"])
    log = (config.data_dir / "logs" / f"{row['id']}.log").read_text(encoding="utf-8")
    assert row["state"] == "published", log
    # Two disjoint 1s subtitle segments, padded at both ends per global settings.
    assert bounds[0] < row["duration"] < bounds[1], log
    assert row["media_name"].endswith(extension)
    item = ET.parse(config.public_dir / "feed.xml").find("./channel/item")
    assert item is not None
    assert item.findtext("title") == "cool show 04"
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert int(enclosure.attrib["length"]) == library.media_path(row).stat().st_size
    assert hashlib.sha256(video.read_bytes()).hexdigest() == checksum
    assert not list((config.data_dir / "staging").iterdir())
    bad = source / "bad 08.mkv"
    bad.write_bytes(b"invalid video")
    os.utime(bad, (old, old))
    library.scan()
    pending = library.episodes("pending")[0]
    asyncio.run(convert(library, pending["id"]))
    assert library.episode(pending["id"])["state"] == "failed"
    assert len(ET.parse(config.public_dir / "feed.xml").findall("./channel/item")) == 1

    audio = library.media_path(row)
    audio_checksum = hashlib.sha256(audio.read_bytes()).hexdigest()
    bad.unlink()
    video.unlink()
    library.scan()
    library.write_feed()
    assert [r["id"] for r in library.episodes()] == [row["id"]]
    assert library.episode(row["id"])["state"] == "published"
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == audio_checksum
    assert len(ET.parse(config.public_dir / "feed.xml").findall("./channel/item")) == 1
