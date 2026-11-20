"""Launcher exit status, argument handling and native Windows containment."""

import os
import subprocess
import sys
import time
from textwrap import dedent
from typing import Any, cast

import pytest

from condensed_podcast.runtime import run_mprocs


def test_launcher_preserves_arguments_cwd_and_exit_status(tmp_path):
    directory = tmp_path / "working directory"
    directory.mkdir()
    code = (
        "import pathlib, sys; "
        "pathlib.Path('argument.txt').write_text(sys.argv[1]); sys.exit(23)"
    )
    argument = 'spaces, "quotes", and a trailing backslash\\'
    assert run_mprocs([sys.executable, "-c", code, argument], cwd=directory) == 23
    assert (directory / "argument.txt").read_text() == argument


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Job Objects required")
@pytest.mark.parametrize("kill_launcher", [False, True])
def test_windows_session_cleans_orphaned_descendants(tmp_path, kill_launcher):
    import win32api
    import win32con
    import win32event
    import win32process

    script = tmp_path / "session stub.py"
    script.write_text(
        dedent("""\
            import os
            import subprocess
            import sys
            import time
            from pathlib import Path
            from condensed_podcast.runtime import run_mprocs

            role = sys.argv[1]
            command = [sys.executable, __file__]
            if role == "launcher":
                sys.exit(run_mprocs(command + ["mprocs"], cwd=Path.cwd()))
            elif role == "mprocs":
                Path("mprocs.pid").write_text(str(os.getpid()))
                # The pane exits, leaving its encoder orphaned inside the job.
                subprocess.run(command + ["pane"], check=True)
                Path("ready").touch()
                deadline = time.monotonic() + 60
                while not Path("stop").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                sys.exit(23)
            elif role == "pane":
                subprocess.Popen(command + ["encoder"])
            elif role == "encoder":
                Path("encoder.pid").write_text(str(os.getpid()))
                Path("encoder-ready").touch()
                time.sleep(60)
            """),
        encoding="utf-8",
    )
    launcher = subprocess.Popen([sys.executable, str(script), "launcher"], cwd=tmp_path)
    processes = []
    try:
        deadline = time.monotonic() + 15
        while not all(
            (tmp_path / name).exists() for name in ("ready", "encoder-ready")
        ):
            assert launcher.poll() is None, "Launcher exited before session startup"
            assert time.monotonic() < deadline, "Session startup timed out"
            time.sleep(0.05)
        for name in ("mprocs.pid", "encoder.pid"):
            handle = win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_TERMINATE,
                False,
                int((tmp_path / name).read_text()),
            )
            processes.append(handle)
            assert win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT
        if kill_launcher:
            launcher.kill()
        else:
            (tmp_path / "stop").touch()
        result = launcher.wait(timeout=15)
        if not kill_launcher:
            assert result == 23  # Closing the job must not kill the launcher itself.
        for handle in processes:
            assert (
                win32event.WaitForSingleObject(handle, 10_000)
                == win32event.WAIT_OBJECT_0
            )
    finally:
        if launcher.poll() is None:
            launcher.kill()
        launcher.wait(timeout=15)
        for handle in processes:
            if win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT:
                win32process.TerminateProcess(handle, 1)
            # pywin32 stubs type OpenProcess's PyHANDLE as int.
            cast(Any, handle).Close()
