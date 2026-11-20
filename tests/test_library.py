import asyncio
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
import tomlkit
from conftest import video_stub

from condensed_podcast.config import load_config
from condensed_podcast.library import Library
from condensed_podcast.runtime import convert, library_lock
from condensed_podcast.sync import parse_actions


def snapshot(config, row, *, action="play", position=90, total=90, timestamp=None):
    event = {
        "podcast": config.feed_url,
        "episode": config.media_url(row["media_name"]),
        "action": action,
        "position": position,
        "total": total,
        "timestamp": timestamp or "2020-01-01T12:00:00Z",
    }
    return {"actions": [event]}


def apply(library, config, row, **kwargs):
    library.apply_actions(
        parse_actions(snapshot(config, row, **kwargs), config.feed_url), time.time()
    )


def feed(config):
    return ET.parse(config.public_dir / "feed.xml").findall("./channel/item")


def test_scanning_title_fallbacks_missing_roots_and_nested_dedup(
    library, config, caplog
):
    source = config.sources[0]
    video_stub(source)
    video_stub(source, "Release - 03 [group 45].mp4")
    video_stub(source, "no-number.mkv")
    nested = source / "another show"
    video_stub(nested, "ep 09.mkv")
    library.config = replace(config, sources=(source / "missing", source, nested))
    library.scan()
    assert {r["title"] for r in library.episodes()} == {
        "cool show 07",
        "cool show 45",
        "another show 09",
    }
    assert "Cannot scan directory" in caplog.text
    assert "no episode-number match" in caplog.text
    identities = {r["id"] for r in library.episodes()}
    library.config = replace(config, sources=(source / "disconnected",))
    library.scan()
    assert not library.episodes()
    library.config = config
    library.scan()
    assert {r["id"] for r in library.episodes()} == identities


def test_unreadable_subdirectory_does_not_block_others(
    library, config, monkeypatch, caplog
):
    source = config.sources[0]
    video_stub(source)
    walk = os.walk

    def inaccessible(path, **kwargs):
        kwargs["onerror"](PermissionError("denied subdirectory"))
        yield from walk(path, **kwargs)

    monkeypatch.setattr(os, "walk", inaccessible)
    library.scan()
    assert [r["title"] for r in library.episodes()] == ["cool show 07"]
    assert "denied subdirectory" in caplog.text


@pytest.mark.parametrize("missing", ["file", "directory"])
@pytest.mark.parametrize("detect_during", ["scan", "conversion"])
def test_unavailable_queued_media_is_forgotten_then_rediscovered(
    library, config, missing, detect_during
):
    source = config.sources[0]
    file = video_stub(source)
    library.scan()
    row = library.episodes()[0]
    stage = config.data_dir / "staging" / row["id"]
    stage.mkdir()
    (stage / "partial.ogg").write_bytes(b"interrupted conversion")
    original = file if missing == "file" else source
    unavailable = original.with_name(original.name + ".unavailable")
    original.rename(unavailable)
    if detect_during == "scan":
        library.scan()
    else:
        asyncio.run(convert(library, row["id"]))
    assert not library.episodes()
    assert not stage.exists()
    unavailable.rename(original)
    library.scan()
    assert library.episodes("pending")[0]["id"] == row["id"]
    assert file.read_bytes() == b"original media, never delete"


def test_inaccessible_directory_discards_queue_without_blocking_other_sources(
    library, config, monkeypatch, caplog
):
    source = config.sources[0]
    video_stub(source)
    library.scan()
    row = library.episodes()[0]
    other = source.with_name("other show")
    video_stub(other)
    library.config = replace(config, sources=(source, other))
    walk = os.walk

    def inaccessible(path, **kwargs):
        if path == source:
            kwargs["onerror"](PermissionError("denied source directory"))
            return
        yield from walk(path, **kwargs)

    monkeypatch.setattr(os, "walk", inaccessible)
    library.scan()
    assert [r["title"] for r in library.episodes()] == ["other show 07"]
    assert "denied source directory" in caplog.text
    monkeypatch.setattr(os, "walk", walk)
    library.scan()
    assert row["id"] in {r["id"] for r in library.episodes("pending")}


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_unreadable_file_is_forgotten_even_when_metadata_is_accessible(
    library, config, monkeypatch, state
):
    file = video_stub(config.sources[0])
    other = video_stub(config.sources[0], "Episode 02.mkv")
    library.scan()
    row = next(r for r in library.episodes() if Path(r["source"]) == file)
    library.update(row["id"], state=state)
    open_file = Path.open

    def inaccessible(path, *args, **kwargs):
        if path == file:
            raise PermissionError("source is not readable")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", inaccessible)
    library.scan()
    assert [Path(r["source"]) for r in library.episodes()] == [other]
    monkeypatch.setattr(Path, "open", open_file)
    library.scan()
    assert row["id"] in {r["id"] for r in library.episodes("pending")}


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_source_access_failure_during_conversion_discards_partial_work(
    library, config, monkeypatch, error
):
    file = video_stub(config.sources[0])
    library.scan()
    row = library.episodes()[0]
    open_file = Path.open

    def inaccessible(path, *args, **kwargs):
        if path == file:
            raise error("source unavailable")
        return open_file(path, *args, **kwargs)

    async def failed_conversion(*args, **kwargs):
        return 1

    monkeypatch.setattr(Path, "open", inaccessible)
    monkeypatch.setattr("condensed_podcast.runtime.run_child", failed_conversion)
    asyncio.run(convert(library, row["id"]))
    assert not library.episodes()
    assert not list((config.data_dir / "staging").iterdir())
    assert not list((config.public_dir / "media").iterdir())


@pytest.mark.parametrize("state", ["published", "trashed", "deleted"])
def test_converted_history_survives_source_disappearance(
    library, config, published, state
):
    if state != "published":
        apply(library, config, published)
    if state == "deleted":
        library.apply_actions(
            parse_actions(snapshot(config, published), config.feed_url),
            time.time() + 7 * 86400 + 1,
        )
    Path(published["source"]).unlink()
    library.scan()
    assert library.episode(published["id"])["state"] == state


def test_config_paths_ordered_patterns_and_global_settings(config):
    assert config.data_dir == config.path.parent / "state with spaces"
    assert config.cover_image is None
    cover = config.path.parent / "my cover.PNG"
    cover.write_bytes(b"image contents")
    document = tomlkit.parse(config.path.read_text())
    document["episodes"] = {"number_patterns": [r".*part-(\d+)", r".*\b(\d\d)\b"]}
    document["shuku"] = {"padding": 1.25, "subtitle_directory": "subtitles"}
    document["feed"] = {"cover_image": cover.name}
    config.path.write_text(tomlkit.dumps(document))
    changed = load_config(config.path)
    assert changed.cover_image == cover
    assert (
        changed.episode_title(changed.sources[0] / "part-123 - 04.mkv")
        == "cool show 123"
    )
    assert changed.episode_title(changed.sources[0] / "release 06.99") == "cool show 06"
    assert changed.episode_title(changed.sources[0] / "release.99") is None
    assert changed.shuku["padding"] == 1.25
    assert changed.shuku["subtitle_directory"] == str(config.path.parent / "subtitles")
    assert changed.shuku["audio_languages"] == ["jpn", "ja"]
    assert changed.shuku["subtitle_languages"] == ["jpn", "ja", "eng", "en"]


@pytest.mark.parametrize(
    "extra",
    [
        {"storage": {"data_dir": "cool show/state"}},
        {"sources": {"directories": ["state with spaces/source"]}},
        {"episodes": {"number_patterns": [r"\d+"]}},
        {"shows": [{"directory": "show", "shuku": {"padding": 2}}]},
        {"server": {"base_url": "http://test.example.ts.net"}},
        {"feed": {"cover_image": "missing.png"}},
        {"feed": {"cover_image": "cover.svg"}},
        {"feed": {"cover_image": ""}},
    ],
)
def test_invalid_config_rejected_before_work(config, extra):
    document = tomlkit.parse(config.path.read_text())
    for key, value in extra.items():
        document[key] = value
    config.path.write_text(tomlkit.dumps(document))
    with pytest.raises(ValueError):
        load_config(config.path)


def test_explicit_invalid_tool_does_not_fall_back(config):
    config = replace(config, tools={"ffmpeg": "not-a-real-ffmpeg"})
    with pytest.raises(ValueError, match="Configured tools.ffmpeg"):
        config.tool("ffmpeg")


@pytest.mark.parametrize("filename", ["config.toml", "nested/custom.toml"])
def test_cli_init_creates_config_without_overwriting(tmp_path, filename):
    command = [sys.executable, "-m", "condensed_podcast"]
    if filename != "config.toml":
        command += ["--config", filename]
    result = subprocess.run(
        command + ["init"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    path = tmp_path / filename
    config = load_config(path)
    assert config.data_dir == path.parent / "data"
    assert config.username == "antennapod"
    assert config.shuku["audio_languages"] == ["jpn", "ja"]

    existing = b"existing user configuration, not necessarily valid TOML\n"
    path.write_bytes(existing)
    result = subprocess.run(
        command + ["init"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert path.read_bytes() == existing


def test_cli_source_editing_preserves_comments_and_is_idempotent(config):
    config.path.write_text("# keep this comment\n" + config.path.read_text())
    directory = config.path.parent / "new show"
    command = [sys.executable, "-m", "condensed_podcast", "--config", str(config.path)]
    for _ in range(2):
        subprocess.run(
            command + ["sources", "add", str(directory)],
            check=True,
            capture_output=True,
        )
    assert load_config(config.path).sources == config.sources + (directory,)
    assert "# keep this comment" in config.path.read_text()
    subprocess.run(
        command + ["sources", "remove", str(directory)], check=True, capture_output=True
    )
    assert load_config(config.path).sources == config.sources


def test_completed_recovery_deadline_then_tombstone(library, config, published):
    original = Path(published["source"]).read_bytes()
    apply(
        library, config, published
    )  # Old offline timestamp must not shorten recovery.
    row = library.episode(published["id"])
    assert row["state"] == "trashed"
    assert abs(row["trashed_at"] - time.time()) < 3
    assert not feed(config)
    assert library.media_path(row, trash=True).read_bytes() == b"audio bytes"
    actions = parse_actions(snapshot(config, row), config.feed_url)
    deadline = row["trashed_at"] + 7 * 86400
    library.apply_actions(actions, deadline - 0.01)
    assert library.episode(row["id"])["state"] == "trashed"
    library.apply_actions(
        {}, deadline + 1
    )  # Even a successful empty poll cannot purge.
    assert library.media_path(row, trash=True).exists()
    library.apply_actions(actions, deadline)
    assert library.episode(row["id"])["state"] == "deleted"
    assert not library.media_path(row, trash=True).exists()
    library.scan()
    assert len(library.episodes()) == 1
    assert not library.episodes("pending")
    assert Path(published["source"]).read_bytes() == original


@pytest.mark.parametrize("kind,position,total", [("new", None, None), ("play", 17, 90)])
def test_later_unplayed_or_partial_restores_stable_feed(
    library, config, published, kind, position, total
):
    before = ET.tostring(feed(config)[0])
    apply(library, config, published)
    apply(
        library,
        config,
        published,
        action=kind,
        position=position,
        total=total,
        timestamp="2026-11-20T10:00:00",
    )
    assert library.episode(published["id"])["state"] == "published"
    assert ET.tostring(feed(config)[0]) == before


def test_manual_restore_ignores_same_completion_but_not_new_completion(
    library, config, published, monkeypatch
):
    apply(library, config, published)
    real_replace = Path.replace

    def locked(path, destination):
        if path.parent == config.data_dir / "trash":
            raise PermissionError("simulated Windows playback lock")
        return real_replace(path, destination)

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", locked)
        library.restore(published["id"])
        assert library.episode(published["id"])["state"] == "restoring"
    # Retrying the same completed snapshot must still finish the pending restore.
    apply(library, config, published)
    apply(library, config, published)
    assert library.episode(published["id"])["state"] == "published"
    apply(library, config, published, timestamp="2026-11-20T10:00:00Z")
    assert library.episode(published["id"])["state"] == "trashed"


@pytest.mark.parametrize(
    "changes",
    [
        {"position": 89, "total": 90},
        {"position": -1},
        {"total": -1},
        {"total": 0},
        {"total": None},
        {"position": float("nan")},
        {"position": True},
        {"action": "download"},
        {"action": "delete"},
        {"action": "future-action"},
        {"timestamp": "not-a-time"},
    ],
)
def test_unsafe_or_incomplete_state_never_authorizes_cleanup(
    library, config, published, changes
):
    payload = snapshot(config, published, **changes)
    try:
        actions = parse_actions(payload, config.feed_url)
    except ValueError:
        actions = {}
    library.apply_actions(actions, time.time() + 90 * 86400)
    assert library.episode(published["id"])["state"] == "published"
    assert library.media_path(published).exists()


def test_duplicate_actions_rejected_and_no_partial_snapshot_applied(config, published):
    payload = snapshot(config, published)
    payload["actions"].append(dict(payload["actions"][0], position=0))
    with pytest.raises(ValueError, match="duplicate"):
        parse_actions(payload, config.feed_url)
    assert parse_actions({"actions": None}, config.feed_url) == {}
    with pytest.raises(ValueError):
        parse_actions({"error": "not authenticated"}, config.feed_url)


def test_locked_move_retries_and_recovery_window_starts_after_success(
    library, config, published, monkeypatch
):
    real_replace = Path.replace

    def locked(path, destination):
        if path.name == published["media_name"]:
            raise PermissionError("simulated Windows lock")
        return real_replace(path, destination)

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", locked)
        apply(library, config, published)
        row = library.episode(published["id"])
        assert row["state"] == "trashing"
        assert row["trashed_at"] is None
        assert library.media_path(row).exists()
    library.recover()
    row = library.episode(published["id"])
    assert row["state"] == "trashed"
    assert abs(row["trashed_at"] - time.time()) < 3


def test_crash_after_move_recovers_without_deleting(library, config, published):
    library.update(published["id"], state="trashing")
    library.media_path(published).replace(library.media_path(published, trash=True))
    library.recover()
    row = library.episode(published["id"])
    assert row["state"] == "trashed"
    library.update(row["id"], state="purging", trashed_at=time.time() - 30 * 86400)
    library.recover()
    assert library.episode(row["id"])["state"] == "trashed"
    assert library.media_path(row, trash=True).exists()


def test_single_manager_lock_and_stable_base_url(config):
    with library_lock(config):
        with pytest.raises(ValueError, match="already running"):
            with library_lock(config):
                pass
    Library(config).close()
    with pytest.raises(ValueError, match="base_url changed"):
        Library(replace(config, base_url="https://renamed.example.ts.net"))


def test_published_metadata_and_settings_changes_do_not_requeue(
    library, config, published
):
    library.config = replace(config, shuku=config.shuku | {"padding": 1.5})
    library.scan()
    row = library.episode(published["id"])
    assert row["state"] == "published"
    item = feed(config)[0]
    assert item.findtext("title") == "cool show 07"
    assert item.findtext("guid") == "urn:uuid:" + row["id"]
    assert item.find("enclosure").attrib == {
        "url": config.media_url(row["media_name"]),
        "length": "11",
        "type": "audio/ogg",
    }


@pytest.mark.parametrize("extension", [".png", ".JPEG"])
def test_feed_cover_is_copied_versioned_and_optional(library, config, extension):
    itunes_image = "{http://www.itunes.com/dtds/podcast-1.0.dtd}image"

    def channel():
        return ET.parse(config.public_dir / "feed.xml").find("channel")

    library.write_feed()
    assert channel().find("image") is None
    assert channel().find(itunes_image) is None
    cover = config.path.parent / ("my cover" + extension)
    # Binary copies must not transcode or change newlines.
    content = b"\xff\x00\r\nimage bytes"
    cover.write_bytes(content)
    library.config = replace(config, cover_image=cover)
    library.write_feed()
    url = channel().find(itunes_image).get("href")
    assert url.startswith(config.media_url("cover" + extension.lower()) + "?v=")
    assert channel().findtext("image/url") == url
    assert channel().findtext("image/title") == config.title
    assert channel().findtext("image/link") == config.feed_url
    served_copy = config.public_dir / "media" / ("cover" + extension.lower())
    assert served_copy.read_bytes() == content
    assert cover.read_bytes() == content

    cover.write_bytes(content + b"updated")
    library.write_feed()
    assert channel().find(itunes_image).get("href") != url
    assert served_copy.read_bytes() == content + b"updated"

    library.config = config
    library.write_feed()
    assert channel().find("image") is None
    assert channel().find(itunes_image) is None
    assert cover.read_bytes() == content + b"updated"
