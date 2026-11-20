"""Load and validate configuration, paths, tool locations and episode naming."""

import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

DEFAULT_PATTERNS = [r".*[Ss]\d{2}[Ee](\d{2})\b", r".*\b(\d\d)\b"]
DEFAULT_SHUKU = {
    "audio_languages": ["jpn", "ja"],
    "subtitle_languages": ["jpn", "ja", "eng", "en"],
}


def atomic_write(path: Path, text: str | bytes) -> None:
    """Replace on the same volume, closing handles first for Windows."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb" if isinstance(text, bytes) else "w",
            encoding=None if isinstance(text, bytes) else "utf-8",
            dir=path.parent,
            delete=False,
        ) as stream:
            name = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        name.replace(path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def number(value: object, label: str, minimum: float = 0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ValueError(f"{label} must be a finite number >= {minimum}")
    return float(value)


@dataclass(frozen=True)
class Config:
    path: Path
    base_url: str
    data_dir: Path
    sources: tuple[Path, ...]
    patterns: tuple[re.Pattern, ...]
    shuku: dict
    tools: dict
    title: str
    description: str
    cover_image: Path | None
    username: str
    password: str
    scan_interval: float
    sync_interval: float
    recovery_days: float
    caddy_port: int
    gopodder_port: int

    @property
    def feed_url(self) -> str:
        return self.base_url + "/podcast/feed.xml"

    @property
    def public_dir(self) -> Path:
        return self.data_dir / "public"

    @property
    def generated_dir(self) -> Path:
        return self.data_dir / "generated"

    def media_url(self, name: str) -> str:
        return self.base_url + "/podcast/media/" + name

    def source_for(self, file: Path) -> Path | None:
        matches = [s for s in self.sources if file.is_relative_to(s)]
        return max(matches, key=lambda p: len(p.parts), default=None)

    def episode_title(self, file: Path) -> str | None:
        source = self.source_for(file)
        if source is None:
            return None
        for pattern in self.patterns:
            match = pattern.match(file.stem)
            if match and match[1]:
                return f"{source.name} {match[1]}"
        return None

    def tool(self, name: str) -> str:
        configured = self.tools.get(name)
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                candidate = self.path.parent / candidate
            candidate = candidate.resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise ValueError(
                    f"Configured tools.{name} is not executable: {candidate}"
                )
            return str(candidate)
        found = shutil.which(name)
        if not found:
            raise ValueError(f"Cannot find {name} on PATH; set tools.{name}")
        return found

    def media_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        ffmpeg = Path(self.tool("ffmpeg"))
        if self.tools.get("ffmpeg"):
            probe = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            expected = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            if ffmpeg.name.lower() != expected or not probe.is_file():
                raise ValueError(
                    "tools.ffmpeg must have ffprobe beside it in its bin directory"
                )
            env["PATH"] = str(ffmpeg.parent) + os.pathsep + env.get("PATH", "")
        if not shutil.which("ffprobe", path=env.get("PATH")):
            raise ValueError("Cannot find ffprobe alongside FFmpeg or on PATH")
        return env


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    with path.open(encoding="utf-8") as stream:
        raw = tomlkit.load(stream).unwrap()
    allowed = {
        "server": {"base_url", "caddy_port", "gopodder_port"},
        "storage": {"data_dir"},
        "sources": {"directories"},
        "feed": {"title", "description", "cover_image"},
        "tools": {"caddy", "ffmpeg", "gopodder", "mprocs", "tailscale"},
        "sync": {"username", "password", "interval_seconds"},
        "scan": {"interval_seconds"},
        "cleanup": {"recovery_days"},
        "episodes": {"number_patterns"},
        "shuku": None,
    }
    for section, values in raw.items():
        if section not in allowed or not isinstance(values, dict):
            raise ValueError(f"Unknown or invalid configuration section: {section}")
        if allowed[section] is not None:
            extra = values.keys() - allowed[section]
            if extra:
                raise ValueError(
                    f"Unknown {section} settings: {', '.join(sorted(extra))}"
                )

    def section(name: str) -> dict:
        return raw.get(name, {})

    def text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")
        return value

    def local(value: object, label: str) -> Path:
        p = Path(text(value, label)).expanduser()
        return (path.parent / p).resolve() if not p.is_absolute() else p.resolve()

    base = text(section("server").get("base_url"), "server.base_url").rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
        or url.port not in (None, 443)
    ):
        raise ValueError(
            "server.base_url must be an HTTPS origin on port 443, without a path"
        )
    data = local(section("storage").get("data_dir", "data"), "storage.data_dir")
    directories = section("sources").get("directories", [])
    if not isinstance(directories, list):
        raise ValueError("sources.directories must be an array of paths")
    sources = tuple(dict.fromkeys(local(p, "source directory") for p in directories))
    for source in sources:
        if data.is_relative_to(source) or source.is_relative_to(data):
            raise ValueError(
                f"data_dir and source directories must not overlap: {source}"
            )
    if path.is_relative_to(data / "public"):
        raise ValueError("Keep the user configuration outside data_dir/public")
    expressions = section("episodes").get("number_patterns", DEFAULT_PATTERNS)
    if not isinstance(expressions, list) or not expressions:
        raise ValueError("episodes.number_patterns must be a nonempty array")
    patterns = []
    for expression in expressions:
        try:
            pattern = re.compile(text(expression, "episode pattern"))
        except re.error as e:
            raise ValueError(f"Invalid episode pattern: {e}") from e
        if pattern.groups < 1:
            raise ValueError(f"Episode pattern needs capture group 1: {expression}")
        patterns.append(pattern)
    ports = []
    for key, default in (("caddy_port", 8765), ("gopodder_port", 8766)):
        port = section("server").get(key, default)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError(f"server.{key} must be an integer from 1024 to 65535")
        ports.append(port)
    if ports[0] == ports[1]:
        raise ValueError("Caddy and goPodder need distinct internal ports")
    tools = section("tools")
    for key, value in tools.items():
        text(value, f"tools.{key}")
    username = section("sync").get("username", "")
    password = section("sync").get("password", "")
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or "/" in username
    ):
        raise ValueError("sync credentials must be strings; username cannot contain /")
    shuku = DEFAULT_SHUKU | section("shuku")
    if shuku.get("subtitle_directory"):
        shuku["subtitle_directory"] = str(
            local(shuku["subtitle_directory"], "shuku.subtitle_directory")
        )
    cover_image = None
    if "cover_image" in section("feed"):
        cover_image = local(section("feed")["cover_image"], "feed.cover_image")
        if cover_image.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ValueError("feed.cover_image must be a JPEG or PNG file")
        if not cover_image.is_file():
            raise ValueError(f"feed.cover_image does not exist: {cover_image}")
    return Config(
        path=path,
        base_url=base,
        data_dir=data,
        sources=sources,
        patterns=tuple(patterns),
        shuku=shuku,
        tools=tools,
        title=text(section("feed").get("title", "Condensed podcast"), "feed.title"),
        description=text(
            section("feed").get("description", "My condensed shows"), "feed.description"
        ),
        cover_image=cover_image,
        username=username,
        password=password,
        scan_interval=number(
            section("scan").get("interval_seconds", 60), "scan.interval_seconds", 1
        ),
        sync_interval=number(
            section("sync").get("interval_seconds", 10), "sync.interval_seconds", 1
        ),
        recovery_days=number(
            section("cleanup").get("recovery_days", 7), "cleanup.recovery_days", 1
        ),
        caddy_port=ports[0],
        gopodder_port=ports[1],
    )
