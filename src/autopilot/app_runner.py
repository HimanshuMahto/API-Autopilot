"""Start the app under test for a run, and stop it afterwards (target.start)."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

import httpx

from .config import Config, ConfigError, StartConfig


class AppStartError(ConfigError):
    pass


def is_up(url: str, timeout: float = 2.0) -> bool:
    """True if anything answers HTTP at the URL. A 404 still means the app is up."""
    try:
        httpx.get(url, timeout=timeout, follow_redirects=False)
        return True
    except httpx.HTTPError:
        return False


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


class RunningApp:
    """Context manager: start the app unless it's already up; stop what we started.

    Output goes to .autopilot/app.log. The app runs in its own process group, so
    stopping it also stops any workers or reloaders it spawned.
    """

    def __init__(
        self,
        start: StartConfig,
        base_url: str,
        root: Path,
        log_path: Path,
        on_event: Callable[[str], None] | None = None,
    ):
        self.start = start
        self.ready_url = start.ready_url or base_url
        if not self.ready_url.startswith(("http://", "https://")):
            self.ready_url = base_url.rstrip("/") + "/" + self.ready_url.lstrip("/")
        self.root = root
        self.log_path = log_path
        self.emit = on_event or (lambda _msg: None)
        self.process: subprocess.Popen[bytes] | None = None
        self.reused = False

    def __enter__(self) -> RunningApp:
        if is_up(self.ready_url):
            self.reused = True
            self.emit(f"App already running at {self.ready_url}; using it.")
            return self
        cwd = (self.root / self.start.cwd).resolve()
        if not cwd.is_dir():
            raise AppStartError(f"target.start.cwd does not exist: {cwd}")
        args: str | list[str] = self.start.command if self.start.shell else shlex.split(self.start.command)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("wb")
        self.emit(f"Starting the app: {self.start.command}")
        try:
            self.process = subprocess.Popen(
                args,
                cwd=cwd,
                shell=self.start.shell,
                env={**os.environ, **self.start.env},
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
            )
        except (OSError, ValueError) as exc:
            log.close()
            raise AppStartError(f"Could not start the app ({self.start.command}): {exc}") from None
        finally:
            log.close()  # the child keeps its own handle

        deadline = time.monotonic() + self.start.timeout_s
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                self.process = None
                raise AppStartError(
                    f"The app exited with code {code} before it was ready. Last lines of "
                    f"{self.log_path}:\n{_tail(self.log_path)}"
                )
            if is_up(self.ready_url, timeout=1.0):
                self.emit(f"App is up at {self.ready_url}.")
                return self
            time.sleep(0.4)
        self._stop()
        raise AppStartError(
            f"The app didn't answer at {self.ready_url} within {self.start.timeout_s:.0f}s. "
            f"Last lines of {self.log_path}:\n{_tail(self.log_path)}"
        )

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self.process is not None:
            self.emit("Stopping the app.")
            self._stop()

    def _stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=8)
        except (ProcessLookupError, PermissionError):
            return
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)


class _NoApp:
    reused = False

    def __enter__(self) -> _NoApp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def app_for(config: Config, on_event: Callable[[str], None] | None = None) -> RunningApp | _NoApp:
    """The app context for a run: starts target.start if configured, else does nothing."""
    if config.target.start is None:
        return _NoApp()
    return RunningApp(
        config.target.start,
        config.target.base_url,
        config.root,
        config.state_dir / "app.log",
        on_event,
    )
