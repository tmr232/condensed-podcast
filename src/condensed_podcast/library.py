"""Discover source media and manage podcast metadata, feeds and recoverable audio."""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path

from .config import Config, atomic_write, path_key

log = logging.getLogger(__name__)
DATA_DIRECTORIES = ("public/media", "trash", "staging", "logs", "generated")
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v", ".ts"}
AUDIO_MIME_TYPES = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mkv": "audio/x-matroska",
}


class Library:
    """Owns generated files only. Source media is always read-only."""

    def __init__(self, config: Config):
        self.config = config
        for directory in DATA_DIRECTORIES:
            (config.data_dir / directory).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.data_dir / "library.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, fingerprint TEXT NOT NULL,
                published_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                media_name TEXT, size INTEGER, duration REAL,
                trashed_at REAL, last_action TEXT, ignored_action TEXT
            );
        """)
        old = self.db.execute(
            "SELECT value FROM settings WHERE key='base_url'"
        ).fetchone()
        if old and old[0] != config.base_url:
            self.db.close()
            raise ValueError(
                "base_url changed for an existing library; restore the original URL"
            )
        self.db.execute(
            "INSERT OR IGNORE INTO settings VALUES ('base_url', ?)", (config.base_url,)
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def episodes(self, *states: str) -> list[sqlite3.Row]:
        sql = "SELECT * FROM episodes"
        if states:
            sql += " WHERE state IN (" + ",".join("?" for _ in states) + ")"
        return self.db.execute(
            sql + " ORDER BY published_at DESC, id", states
        ).fetchall()

    def conversion_queue(self) -> list[sqlite3.Row]:
        """Pending work in source-directory order, then numeric episode order."""

        def priority(row: sqlite3.Row) -> tuple:
            show = self.config.source_for(Path(row["source"]))
            # Titles already contain the number selected by the configured regexes.
            number = row["title"].rsplit(" ", 1)[-1]
            return (
                self.config.sources.index(show)
                if show is not None
                else len(self.config.sources),
                int(number) if number.isdecimal() else float("inf"),
                row["title"],
                row["id"],
            )

        return sorted(self.episodes("pending"), key=priority)

    def episode(self, episode_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown episode: {episode_id}")
        return row

    def update(self, episode_id: str, **values) -> None:
        # Keys are internal column names, never user input.
        self.db.execute(
            "UPDATE episodes SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), episode_id),
        )
        self.db.commit()

    def fingerprint(self, file: Path) -> str:
        stat = file.stat()
        subtitles = []
        candidates = set(file.parent.iterdir())
        if external := self.config.shuku.get("subtitle_directory"):
            location = Path(external)
            if location.is_file():
                candidates.add(location)
            else:
                candidates.update(location.iterdir())
        for sidecar in sorted(candidates):
            if sidecar.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt"}:
                s = sidecar.stat()
                subtitles.append((str(sidecar), s.st_size, s.st_mtime_ns))
        payload = (stat.st_size, stat.st_mtime_ns, subtitles, self.config.shuku)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def scan(self) -> None:
        """Rediscover unconverted work without discarding converted history."""
        seen = set()

        def warning(error: OSError) -> None:
            log.warning("Cannot scan directory; keeping it configured: %s", error)

        for source in self.config.sources:
            for root, dirs, files in os.walk(
                source, onerror=warning, followlinks=False
            ):
                # Don't follow junctions or symlinks out of a configured tree.
                dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
                for name in sorted(files):
                    file = Path(root) / name
                    if file.suffix.lower() not in MEDIA_EXTENSIONS or file.is_symlink():
                        continue
                    try:
                        file = file.resolve()
                        key = path_key(file)
                        if key in seen or self.config.source_for(file) is None:
                            continue
                        self._discover(file, key)
                        seen.add(key)
                    except OSError as error:
                        log.warning("Skipping inaccessible media %s: %s", file, error)

        for row in self.episodes("pending", "failed"):
            if row["source"] not in seen:
                self.discard_unconverted(row["id"])

    def discard_unconverted(self, episode_id: str) -> None:
        """Forget unavailable work; a later scan can discover it again."""
        row = self.episode(episode_id)
        if row["state"] not in {"pending", "failed"}:
            return
        stage = self.config.data_dir / "staging" / episode_id
        if stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError as error:
                log.warning("Discarded episode staging cleanup deferred: %s", error)
        self.db.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        self.db.commit()
        log.info("Forgot unavailable source: %s", row["source"])

    def _discover(self, file: Path, key: str) -> None:
        """Register a stable input or requeue changed, unpublished media."""
        row = self.db.execute(
            "SELECT * FROM episodes WHERE source=?", (key,)
        ).fetchone()
        if row is not None and row["state"] not in {"pending", "failed"}:
            return
        title = self.config.episode_title(file)
        if title is None:
            log.warning("Skipping media with no episode-number match: %s", file)
            return
        stat = file.stat()
        if time.time() - stat.st_mtime < 5:
            return  # Don't start on a file still being copied.
        with file.open("rb") as stream:
            stream.read(1)
        fingerprint = self.fingerprint(file)
        episode_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        if row is None:
            birth = getattr(stat, "st_birthtime", None)
            if birth is None and os.name == "nt":
                birth = stat.st_ctime
            self.db.execute(
                """INSERT INTO episodes
                (id,source,title,fingerprint,published_at)
                VALUES (?,?,?,?,?)""",
                (episode_id, key, title, fingerprint, birth or time.time()),
            )
            self.db.commit()
            log.info("Discovered %s", title)
        elif fingerprint != row["fingerprint"] or title != row["title"]:
            self.update(
                row["id"], title=title, fingerprint=fingerprint, state="pending"
            )

    def media_path(self, row: sqlite3.Row, trash: bool = False) -> Path:
        directory = (
            self.config.data_dir / "trash"
            if trash
            else self.config.public_dir / "media"
        )
        return directory / row["media_name"]

    def finish_move(self, episode_id: str) -> None:
        row = self.episode(episode_id)
        restoring = row["state"] == "restoring"
        source = self.media_path(row, trash=restoring)
        destination = self.media_path(row, trash=not restoring)
        try:
            if source.exists():
                source.replace(destination)
            elif not destination.exists():
                raise FileNotFoundError(
                    f"Both generated copies are missing: {episode_id}"
                )
            self.update(
                episode_id,
                state="published" if restoring else "trashed",
                trashed_at=None if restoring else time.time(),
            )
        except OSError as error:
            log.warning("Move deferred (possibly a file lock): %s", error)

    def restore(self, episode_id: str) -> None:
        row = self.episode(episode_id)
        if row["state"] not in {"trashed", "trashing", "restoring", "purging"}:
            raise ValueError("Only recoverable episodes can be restored")
        self.update(episode_id, state="restoring", ignored_action=row["last_action"])
        self.finish_move(episode_id)
        self.write_feed()

    def recover(self) -> None:
        for row in self.episodes("processing", "trashing", "restoring", "purging"):
            match row["state"]:
                case "trashing" | "restoring":
                    self.finish_move(row["id"])
                case "purging":
                    # A fresh sync must precede further irreversible deletion.
                    state = (
                        "trashed"
                        if self.media_path(row, trash=True).exists()
                        else "deleted"
                    )
                    self.update(row["id"], state=state)
                case "processing":
                    if row["media_name"] and self.media_path(row).is_file():
                        self.update(row["id"], state="published")
                    else:
                        self.update(row["id"], state="pending")
        self.write_feed()

    def apply_actions(self, actions: dict[str, tuple[bool, str]], now: float) -> None:
        """Only call with a fully validated, successfully fetched sync snapshot."""
        changed = False
        for row in self.episodes(
            "published", "trashed", "trashing", "restoring", "purging"
        ):
            action = actions.get(self.config.media_url(row["media_name"]))
            if action is None:
                continue  # Missing state is not permission to purge or restore.
            completed, signature = action
            self.update(row["id"], last_action=signature)
            if row["state"] == "restoring":
                self.finish_move(row["id"])
                changed = True
                continue
            if not completed:
                if row["state"] != "published":
                    self.restore(row["id"])
                    changed = True
                continue
            if signature == row["ignored_action"]:
                continue
            match row["state"]:
                case "published":
                    self.update(row["id"], state="trashing")
                    self.finish_move(row["id"])
                    changed = True
                case "trashing":
                    self.finish_move(row["id"])
                    changed = True
                case "trashed" | "purging" if row["trashed_at"] is not None and (
                    now - row["trashed_at"] >= self.config.recovery_days * 86400
                ):
                    self.update(row["id"], state="purging")
                    try:
                        self.media_path(row, trash=True).unlink(missing_ok=True)
                        self.update(row["id"], state="deleted")
                        log.info(
                            "Permanently removed generated audio: %s", row["title"]
                        )
                    except OSError as error:
                        self.update(row["id"], state="trashed")
                        log.warning("Purge deferred: %s", error)
        if changed:
            self.write_feed()

    def write_feed(self) -> None:
        ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
        ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        for key, value in (
            ("title", self.config.title),
            ("link", self.config.feed_url),
            ("description", self.config.description),
            ("language", "ja"),
        ):
            ET.SubElement(channel, key).text = value
        ET.SubElement(
            channel,
            "{http://www.w3.org/2005/Atom}link",
            {
                "href": self.config.feed_url,
                "rel": "self",
                "type": "application/rss+xml",
            },
        )
        if self.config.cover_image is not None:
            content = self.config.cover_image.read_bytes()
            name = "cover" + self.config.cover_image.suffix.lower()
            atomic_write(self.config.public_dir / "media" / name, content)
            # A new URL lets clients refresh artwork even when its path is unchanged.
            url = (
                self.config.media_url(name)
                + "?v="
                + hashlib.sha256(content).hexdigest()
            )
            ET.SubElement(
                channel,
                "{http://www.itunes.com/dtds/podcast-1.0.dtd}image",
                href=url,
            )
            image = ET.SubElement(channel, "image")
            for key, value in (
                ("url", url),
                ("title", self.config.title),
                ("link", self.config.feed_url),
            ):
                ET.SubElement(image, key).text = value
        for row in self.episodes("published"):
            if not self.media_path(row).is_file():
                log.warning(
                    "Published audio is missing, retaining record: %s", row["title"]
                )
                continue
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = row["title"]
            ET.SubElement(item, "description").text = row["title"]
            ET.SubElement(item, "guid", isPermaLink="false").text = (
                "urn:uuid:" + row["id"]
            )
            ET.SubElement(item, "pubDate").text = formatdate(
                row["published_at"], usegmt=True
            )
            extension = Path(row["media_name"]).suffix
            ET.SubElement(
                item,
                "enclosure",
                {
                    "url": self.config.media_url(row["media_name"]),
                    "length": str(row["size"]),
                    "type": AUDIO_MIME_TYPES[extension],
                },
            )
            ET.SubElement(
                item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"
            ).text = str(max(1, round(row["duration"])))
        ET.indent(rss)
        xml = ET.tostring(rss, encoding="unicode", xml_declaration=True)
        atomic_write(self.config.public_dir / "feed.xml", xml)
