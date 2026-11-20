import os
import time
from pathlib import Path

import pytest
import tomlkit

from condensed_podcast.config import load_config
from condensed_podcast.library import Library


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "cool show"
    source.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        tomlkit.dumps(
            {
                "server": {"base_url": "https://test.example.ts.net"},
                "storage": {"data_dir": "state with spaces"},
                "sources": {"directories": [str(source)]},
                "sync": {"username": "phone", "password": "test-only-password"},
            }
        ),
        encoding="utf-8",
    )
    return load_config(path)


@pytest.fixture
def library(config):
    library = Library(config)
    yield library
    library.close()


def video_stub(source: Path, name: str = "Release S02E07 - 10 bit.mkv") -> Path:
    file = source / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"original media, never delete")
    old = time.time() - 60
    os.utime(file, (old, old))
    return file


@pytest.fixture
def published(library, config):
    video_stub(config.sources[0])
    library.scan()
    row = library.episodes()[0]
    name = row["id"] + ".ogg"
    library.update(
        row["id"], state="published", media_name=name, size=11, duration=31.5
    )
    row = library.episode(row["id"])
    library.media_path(row).write_bytes(b"audio bytes")
    library.write_feed()
    return row
