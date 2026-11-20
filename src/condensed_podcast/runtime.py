"""Generate service configs and run conversion, synchronization and private serving."""

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

from .config import Config, atomic_write, load_config
from .library import AUDIO_MIME_TYPES, Library
from .sync import fetch_actions

log = logging.getLogger(__name__)


@contextlib.contextmanager
def library_lock(config: Config):
    """One file-owning process, including manual restore/retry commands."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    with (config.data_dir / "manager.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(
                "Manager is already running; stop the mprocs session first"
            ) from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run_mprocs(command: list[str], *, cwd: Path) -> int:
    """Own the entire Windows session, including descendants orphaned by panes."""
    if os.name != "nt":
        return subprocess.call(command, cwd=cwd)
    import win32con
    import win32event
    import win32job
    import win32process

    with contextlib.ExitStack() as handles:
        # pywin32 requires a string here, not None. Empty means an unnamed job.
        job = win32job.CreateJobObject(None, "")
        handles.callback(job.Close)
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        # Suspend before assignment so no child can escape containment at startup.
        # Inherit the console, but no handles (especially not the job handle).
        process, thread, _, _ = win32process.CreateProcess(
            None,
            subprocess.list2cmdline(command),
            None,
            None,
            False,
            win32con.CREATE_SUSPENDED,
            None,
            str(cwd),
            win32process.STARTUPINFO(),
        )
        handles.callback(process.Close)
        handles.callback(thread.Close)
        try:
            win32job.AssignProcessToJobObject(job, process)
            win32process.ResumeThread(thread)
        except BaseException:
            # Assignment can fail before the job owns this suspended process.
            win32process.TerminateProcess(process, 1)
            raise
        # A finite wait lets Python handle Ctrl+C and close the job promptly.
        while win32event.WaitForSingleObject(process, 100) != win32event.WAIT_OBJECT_0:
            pass
        return win32process.GetExitCodeProcess(process)


def shuku_settings(config: Config, output: Path) -> dict:
    # shuku accepts flattened TOML keys, except its custom_ffmpeg_args maps.
    flat = {}

    def flatten(values: dict, prefix: str = "") -> None:
        for key, value in values.items():
            full = prefix + key
            if isinstance(value, dict) and not full.endswith("custom_ffmpeg_args"):
                flatten(value, full + ".")
            else:
                flat[full] = value

    flatten(config.shuku)
    flat.update(
        {
            "output_directory": str(output),
            "output_suffix": ".condensed",
            "if_file_exists": "overwrite",
            "condensed_audio.enabled": True,
            "condensed_video.enabled": False,
            "condensed_subtitles.enabled": False,
        }
    )
    return flat


def generate(config: Config) -> Path:
    config.generated_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (config.public_dir / "media").mkdir(parents=True, exist_ok=True)
    proxy = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"127.0.0.1:{config.gopodder_port}"}],
        "headers": {
            "request": {
                "set": {
                    "X-Forwarded-Proto": ["https"],
                    "X-Forwarded-Host": [urlsplit(config.base_url).netloc],
                    "Host": [urlsplit(config.base_url).netloc],
                }
            }
        },
    }
    caddy = {
        "admin": {"disabled": True, "config": {"persist": False}},
        "apps": {
            "http": {
                "servers": {
                    "podcast": {
                        "listen": [f"127.0.0.1:{config.caddy_port}"],
                        "routes": [
                            {
                                "match": [
                                    {"path": ["/podcast/feed.xml", "/podcast/media/*"]}
                                ],
                                "handle": [
                                    {
                                        "handler": "rewrite",
                                        "strip_path_prefix": "/podcast",
                                    },
                                    {
                                        "handler": "file_server",
                                        "root": str(config.public_dir),
                                    },
                                ],
                                "terminal": True,
                            },
                            {"handle": [proxy]},
                        ],
                    }
                }
            }
        },
    }
    atomic_write(config.generated_dir / "caddy.json", json.dumps(caddy, indent=2))
    atomic_write(
        config.generated_dir / "shuku.toml",
        tomlkit.dumps(shuku_settings(config, config.data_dir / "staging")),
    )
    python = [sys.executable, "-m", "condensed_podcast", "--config", str(config.path)]
    processes = {
        "caddy": [
            config.tool("caddy"),
            "run",
            "--config",
            str(config.generated_dir / "caddy.json"),
        ],
        "gopodder": [
            config.tool("gopodder"),
            "serve",
            "--db-backend",
            "sqlite",
            "--debug-address",
            "",
            "--listen-address",
            f"127.0.0.1:{config.gopodder_port}",
            "--db-path",
            str(config.data_dir / "gopodder.sqlite3"),
        ],
        "tailscale": python + ["serve"],
        "manager": python + ["worker"],
    }
    mprocs = {"procs": {name: {"cmd": cmd} for name, cmd in processes.items()}}
    path = config.generated_dir / "mprocs.yaml"
    atomic_write(
        path, json.dumps(mprocs, indent=2)
    )  # JSON is also YAML; argv, never shell strings.
    return path


def serve(config: Config) -> int:
    """Refuse to overwrite somebody else's Serve/Funnel configuration."""
    executable = config.tool("tailscale")
    status = subprocess.run(
        [executable, "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    own = json.loads(status.stdout).get("Self", {})
    hostname = own.get("DNSName", "").rstrip(".").lower()
    if not hostname or hostname != urlsplit(config.base_url).hostname.lower():
        raise ValueError(
            "server.base_url must match this machine's Tailscale HTTPS DNS name"
        )
    status = subprocess.run(
        [executable, "serve", "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    mappings = json.loads(status.stdout) or {}
    if any(
        mappings.get(key)
        for key in (
            "TCP",
            "Web",
            "AllowFunnel",
            "Services",
            "Foreground",
        )
    ):
        raise ValueError(
            "Existing Tailscale Serve/Funnel configuration; use a dedicated node "
            "or remove only the conflicting mapping yourself. Nothing was changed."
        )
    # Foreground lifecycle: no --bg and, deliberately, never `tailscale funnel`.
    command = [
        executable,
        "serve",
        "--https=443",
        "--yes",
        f"http://127.0.0.1:{config.caddy_port}",
    ]
    if os.name != "nt":
        os.execv(executable, command)
    return subprocess.call(command)


async def run_child(argv: list[str], *, env: dict, cwd: Path, output) -> int:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )
    try:
        return await process.wait()
    finally:
        if process.returncode is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def convert(library: Library, episode_id: str) -> None:
    config = library.config  # One immutable config snapshot for the whole job.
    row = library.episode(episode_id)
    source = Path(row["source"])
    if config.source_for(source) is None:
        return
    stage = config.data_dir / "staging" / episode_id
    output = stage / "output"
    try:
        current = library.fingerprint(source)
        if time.time() - source.stat().st_mtime < 5:
            return
        if stage.exists():
            shutil.rmtree(stage)
        output.mkdir(parents=True)
        atomic_write(
            stage / "shuku.toml", tomlkit.dumps(shuku_settings(config, output))
        )
        env = config.media_environment()
        # Keep shuku's temporary segments under data_dir too, never beside originals.
        env.update({"TEMP": str(stage), "TMP": str(stage), "TMPDIR": str(stage)})
        env["PYTHONUTF8"] = "1"  # shuku prints Unicode even to redirected Windows logs.
        library.update(episode_id, state="processing", fingerprint=current)
        executable = Path(sys.executable).parent / (
            "shuku.exe" if os.name == "nt" else "shuku"
        )
        if not executable.is_file():
            raise ValueError(
                "Project-local shuku is missing; run uv sync and use uv run"
            )
        with (config.data_dir / "logs" / f"{episode_id}.log").open("wb") as logfile:
            log.info("Condensing %s", row["title"])
            code = await run_child(
                [
                    str(executable),
                    "--config",
                    str(stage / "shuku.toml"),
                    str(source),
                ],
                env=env,
                cwd=stage,
                output=logfile,
            )
        if code != 0:
            raise ValueError(f"shuku exited {code}; see logs/{episode_id}.log")
        candidates = [
            file for file in output.iterdir() if file.suffix.lower() in AUDIO_MIME_TYPES
        ]
        if len(candidates) != 1 or candidates[0].stat().st_size == 0:
            raise ValueError(
                f"shuku produced no unambiguous audio; see logs/{episode_id}.log"
            )
        media = candidates[0]
        # ffprobe is short-lived; run off the event loop so sync polling still proceeds.
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(media),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        if (
            not math.isfinite(duration)
            or duration <= 0
            or not any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        ):
            raise ValueError("Generated file has no playable audio duration")
        if library.fingerprint(source) != current:
            library.update(episode_id, state="pending")
            log.warning(
                "Source or subtitles changed during conversion; retrying %s", source
            )
            return
        name = episode_id + media.suffix.lower()
        library.update(
            episode_id, media_name=name, size=media.stat().st_size, duration=duration
        )
        media.replace(config.public_dir / "media" / name)
        library.update(episode_id, state="published")
        library.write_feed()
        log.info("Published %s (%.1fs)", row["title"], duration)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if library.episode(episode_id)["state"] == "published":
            log.warning("Published audio retained; feed update needs retry: %s", error)
        else:
            # Other I/O failures remain retryable if the source is accessible.
            library.update(
                episode_id,
                state="pending" if isinstance(error, OSError) else "failed",
            )
            try:
                library.fingerprint(source)
                with source.open("rb") as stream:
                    stream.read(1)
            except OSError:
                library.discard_unconverted(episode_id)
            log.warning("Skipping %s: %s", source, error)
    finally:
        if stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError as error:
                log.warning("Staging cleanup deferred: %s", error)


async def worker(config: Config) -> None:
    if os.name != "nt":
        # Let cancellation close the conversion process group before mprocs exits.
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM,
            asyncio.current_task().cancel,
        )
    with library_lock(config):
        library = Library(config)
        library.recover()

        def refresh() -> None:
            try:
                updated = load_config(config.path)
                if (
                    updated.data_dir != config.data_dir
                    or updated.base_url != config.base_url
                ):
                    raise ValueError(
                        "Restart required to change storage.data_dir or server.base_url"
                    )
                library.config = updated
            except (ValueError, OSError) as error:
                log.warning("Keeping last valid configuration: %s", error)

        async def scans() -> None:
            while True:
                refresh()
                library.scan()
                for row in library.conversion_queue():
                    refresh()  # Removing a source stops queued work immediately.
                    await convert(library, row["id"])
                library.write_feed()
                await asyncio.sleep(library.config.scan_interval)

        async def sync() -> None:
            while True:
                try:
                    actions = await asyncio.to_thread(fetch_actions, library.config)
                    library.apply_actions(actions, time.time())
                except (OSError, ValueError) as error:
                    log.warning("Cleanup paused; sync unavailable or unsafe: %s", error)
                await asyncio.sleep(library.config.sync_interval)

        tasks = [asyncio.create_task(scans()), asyncio.create_task(sync())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            library.close()
