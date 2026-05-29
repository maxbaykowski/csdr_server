from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from .constants import PR_SET_NAME

_RUNTIME_DIR_CACHE: Path | None = None

def _read_sysfs_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except FileNotFoundError:
        return None


def _set_process_name(name: str) -> None:
    try:
        ctypes.CDLL(None).prctl(PR_SET_NAME, name.encode("utf-8")[:15], 0, 0, 0)
    except Exception:
        pass

def _ensure_runtime_subdir(parent: Path, name: str) -> Path | None:
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None
    candidate = parent / name
    try:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(candidate, 0o700)
    except OSError:
        return None
    if not os.access(candidate, os.W_OK | os.X_OK):
        return None
    return candidate


def _get_runtime_dir() -> Path:
    global _RUNTIME_DIR_CACHE
    if _RUNTIME_DIR_CACHE is not None:
        return _RUNTIME_DIR_CACHE

    candidates: list[Path] = []
    explicit_runtime_dir = os.environ.get("CSDR_SERVER_RUNTIME_DIR")
    if explicit_runtime_dir:
        candidates.append(Path(explicit_runtime_dir))

    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        candidates.append(Path(xdg_runtime_dir) / "csdr_server")

    candidates.append(Path("/run/csdr_server"))

    home = Path.home()
    if str(home) not in {"", "."}:
        candidates.append(home / ".cache" / "csdr_server" / "runtime")

    candidates.append(Path(tempfile.gettempdir()) / f"csdr_server-{os.getuid()}")

    for candidate in candidates:
        runtime_dir = _ensure_runtime_subdir(candidate.parent, candidate.name)
        if runtime_dir is not None:
            _RUNTIME_DIR_CACHE = runtime_dir
            return runtime_dir

    raise OSError(
        errno.EACCES,
        "could not create a writable runtime directory for csdr_server; "
        "set CSDR_SERVER_RUNTIME_DIR or XDG_RUNTIME_DIR to a writable path",
    )


def terminate_process(process: subprocess.Popen[bytes], name: str) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        from .constants import LOGGER

        LOGGER.warning("%s did not exit after SIGTERM, killing it", name)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=2.0)
