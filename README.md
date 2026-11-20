# condensed-podcast

A native-Windows pipeline: videos → shuku condensed audio → private podcast →
AntennaPod → recoverable cleanup. No Docker or WSL. **Originals are never changed.**

## Install

Use Python **3.15+**. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/) and these Windows
executables; verify release checksums:

| Tool | Source | Notes |
| --- | --- | --- |
| Caddy | [Download](https://caddyserver.com/download) | Standard build |
| FFmpeg + ffprobe | [Download](https://ffmpeg.org/download.html#build-windows) | Keep both together |
| goPodder | [v1.4.3](https://github.com/cbrgm/gopodder/releases/tag/v1.4.3) | Windows amd64; not the desktop client |
| mprocs | [v0.9.6](https://github.com/pvolok/dekit/releases/tag/v0.9.6) | Last mprocs-named release; newer versions are dekit |
| Tailscale | [Download](https://tailscale.com/download/windows) | Sign in on PC and phone; enable tailnet HTTPS |

Clone/extract this repository, then run from its directory:

```powershell
uv sync --locked
uv run condensed-podcast init
```

shuku stays project-local. `init` creates a commented `config.toml` without
overwriting existing files. For another location, put `--config PATH` before `init`
(and before subsequent subcommands).

## Configure and start

Edit `config.toml`:

| Setting | Value |
| --- | --- |
| `server.base_url` | This PC's exact Tailscale HTTPS origin, used in feed URLs. Keep it stable. |
| `storage.data_dir` | Local writable data folder, separate from sources. Relative paths resolve beside the config. |
| `sources.directories` | One directory per show; titles become `directory name 01`, etc. |
| `sync` | Credentials for the separate goPodder sync user created below. |
| `tools` | Executable paths, or omit to use PATH. Explicit invalid paths fail; `ffprobe` must accompany an explicit FFmpeg path. |
| `episodes.number_patterns` | Ordered global Python `re.match` patterns against the filename stem; capture group 1 is the number. |
| `shuku` | Global [shuku options](https://github.com/welpo/shuku#configuration). Defaults prefer Japanese audio, then Japanese/English subtitles; normal first-match selection applies. |

There are no per-show overrides. Inputs needing prompts are logged/skipped;
the app controls output locations. Protect config/data with Windows permissions:
the config contains your password. Restrict tailnet access with grants/ACLs.

```powershell
uv run condensed-podcast run
```

This opens mprocs with **Caddy, goPodder, Tailscale Serve and manager**.
Serve is private/foreground, never Funnel; existing mappings are not overwritten.
Caddy/goPodder bind to loopback only—don't expose them through the firewall.
On Windows, the launcher owns a Job Object: exiting mprocs or terminating the
launcher kills all session descendants, including shuku/FFmpeg. Force-stopping
just one pane doesn't provide this session-wide cleanup.

1. Open `server.base_url`; create a goPodder **admin** account.
2. Log in → **gPodder Users** → create the **sync user** matching your config.
   Use these credentials in AntennaPod, not the admin/Tailscale login.
3. Subscribe to `server.base_url/podcast/feed.xml` in AntennaPod. Set its custom
   gpodder.net server to `server.base_url` and explicitly sync.
4. Test played → sync → trash, then unplayed → sync → restore on one test episode.

Keep this one listening device authoritative. The PC must run for serving/sync;
stop all mprocs processes when finished. Until sync succeeds, cleanup is paused.

## Manage shows

```powershell
uv run condensed-podcast sources add 'X:\shows\cool show'
uv run condensed-podcast sources list
uv run condensed-podcast sources remove 'X:\shows\cool show'
uv run condensed-podcast status
uv run condensed-podcast trash list
uv run condensed-podcast trash restore EPISODE-ID
uv run condensed-podcast retry
```

Stop the whole mprocs session for `status`, `trash` and `retry`; restart afterward.
Source changes reload automatically. Removing a source leaves published episodes;
unreachable directories warn and scanning continues. Unconverted entries not
rediscovered by a scan are discarded, including those in unavailable directories;
returning files are discovered again. Converted history stays independent of
originals. Overlapping roots use the most-specific show directory. Failed inputs
retry on input/settings changes or `retry`; published episodes aren't reconverted.

## Cleanup and recovery

- Synced completion (including manual marking) moves generated audio to `trash/`
  and removes its feed entry. Manual-mark sync varies by AntennaPod control:
  test yours. Unsynced phone changes are unknowable; sync before resuming an
  offline PC. A current server response doesn't prove the phone synced.
- Purge requires **seven elapsed days after the successful move** plus a successful
  poll still reporting completion. Offline time counts. Missing/invalid state,
  download/delete actions and auth/network failures never authorize deletion.
- Unplayed/partial sync restores recoverable episodes with unchanged IDs/URLs.
  Manual restore ignores the same completion action until it changes.
  Purged episodes have tombstones preventing regeneration; no built-in undo.

All state lives under `data_dir`; only `public/` is file-served. Don't add arbitrary
files/symlinks there. To relocate data: stop everything, move the entire folder,
update the path and restart with the same base URL. Back up config separately.
Data/port/tool/URL changes require restarting; other settings reload automatically.

## Development

```powershell
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Integration tests need FFmpeg/ffprobe or skip. Native Windows shutdown and real
AntennaPod/Tailscale acceptance still need checking on the target machine.
