"""Initialize configuration and manage services, sources and recoverable episodes."""

import asyncio
import logging
import subprocess
from contextlib import closing, contextmanager
from importlib.resources import files
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated

import tomlkit
import typer

from .config import Config, atomic_write, load_config, path_key
from .library import Library
from .runtime import generate, library_lock, run_mprocs, serve, worker

app = typer.Typer(
    help="A private condensed-audio podcast",
    add_completion=False,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
sources_app = typer.Typer(help="Add, remove and list show directories.")
trash_app = typer.Typer(help="List and restore recoverable episodes.")
app.add_typer(sources_app, name="sources")
app.add_typer(trash_app, name="trash")


@app.callback()
def configure(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option("--config", help="Configuration file.")
    ] = Path("config.toml"),
) -> None:
    # Keep loading lazy: init and command help don't need an existing config.
    ctx.obj = config
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )


def source_command(config: Config, action: str, directory: Path) -> None:
    # CLI paths follow the shell convention; stored values are absolute.
    path = directory.expanduser().resolve()
    original = config.path.read_text(encoding="utf-8")
    document = tomlkit.parse(original)
    sources = document.setdefault("sources", tomlkit.table())
    values = sources.setdefault("directories", tomlkit.array())
    matches = [i for i, p in enumerate(config.sources) if path_key(p) == path_key(path)]
    if action == "add" and not matches:
        if config.data_dir.is_relative_to(path) or path.is_relative_to(config.data_dir):
            raise ValueError("Source directories and data_dir must not overlap")
        values.append(str(path))
    elif action == "remove":
        # Compare original entries, including duplicates, before editing.
        for i in range(len(values) - 1, -1, -1):
            candidate = (config.path.parent / Path(values[i]).expanduser()).resolve()
            if path_key(candidate) == path_key(path):
                del values[i]
    if config.path.read_text(encoding="utf-8") != original:
        raise ValueError("Configuration changed during edit; please retry")
    atomic_write(config.path, tomlkit.dumps(document))
    print(f"Source {action}: {path}")


@sources_app.command("add")
def add_source(ctx: typer.Context, directory: Path) -> None:
    """Add a show directory (it need not currently be accessible)."""
    source_command(load_config(ctx.obj), "add", directory)


@sources_app.command("remove")
def remove_source(ctx: typer.Context, directory: Path) -> None:
    """Remove a show directory without deleting published episodes."""
    source_command(load_config(ctx.obj), "remove", directory)


@sources_app.command("list")
def list_sources(ctx: typer.Context) -> None:
    """List configured show directories."""
    for path in load_config(ctx.obj).sources:
        print(path)


@app.command("init")
def init_command(ctx: typer.Context) -> None:
    """Create a starter config without overwriting files."""
    template = (
        files("condensed_podcast")
        .joinpath("config.example.toml")
        .read_text(encoding="utf-8")
    )
    path = ctx.obj.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(template)
    print(f"Created {path}. Edit it before starting services.")


@app.command("generate")
def generate_command(ctx: typer.Context) -> None:
    """Generate service configuration without starting processes."""
    generated = generate(load_config(ctx.obj))
    print(f"Generated configuration: {generated}", flush=True)


@app.command("run")
def run_command(ctx: typer.Context) -> None:
    """Generate configuration and start everything in mprocs."""
    config = load_config(ctx.obj)
    config.media_environment()
    config.tool("tailscale")
    executable = config.tool("mprocs")
    generated = generate(config)
    print(f"Generated configuration: {generated}", flush=True)
    print(f"Feed: {config.feed_url}", flush=True)
    raise typer.Exit(
        run_mprocs(
            [executable, "--config", str(generated)], cwd=config.data_dir / "logs"
        )
    )


@app.command("serve")
def serve_command(ctx: typer.Context) -> None:
    """Run private foreground Tailscale Serve."""
    raise typer.Exit(serve(load_config(ctx.obj)))


@app.command("worker")
def worker_command(ctx: typer.Context) -> None:
    """Scan, convert, synchronize and clean up episodes."""
    config = load_config(ctx.obj)
    (config.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        config.data_dir / "logs" / "manager.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    asyncio.run(worker(config))


@contextmanager
def open_library(ctx: typer.Context):
    """Share locking, crash recovery and closure across library commands."""
    config = load_config(ctx.obj)
    with library_lock(config), closing(Library(config)) as library:
        library.recover()
        yield library


@app.command("status")
def status_command(ctx: typer.Context) -> None:
    """List all episodes and their current states."""
    with open_library(ctx) as library:
        for row in library.episodes():
            print(f"{row['id']}  {row['state']:10}  {row['title']}")


@app.command("retry")
def retry_command(ctx: typer.Context) -> None:
    """Queue failed episodes for the next manager run."""
    with open_library(ctx) as library:
        for row in library.episodes("failed"):
            library.update(row["id"], state="pending")
        print("Failed episodes queued for the next manager run")


@trash_app.command("list")
def list_trash(ctx: typer.Context) -> None:
    """List episodes whose generated audio is still recoverable."""
    with open_library(ctx) as library:
        for row in library.episodes("trashed", "trashing", "restoring", "purging"):
            print(f"{row['id']}  {row['state']:10}  {row['title']}")


@trash_app.command("restore")
def restore_trash(ctx: typer.Context, episode_id: str) -> None:
    """Restore a recoverable episode by ID."""
    with open_library(ctx) as library:
        library.restore(episode_id)
        restored = library.episode(episode_id)
        print(f"Restore state: {restored['state']}")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt, asyncio.CancelledError:
        pass
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise SystemExit(1) from error
