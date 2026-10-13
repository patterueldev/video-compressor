"""ffmpeg/ffprobe directory detection for the service environment (issue #71).

The worker service runs without an interactive shell, so a Homebrew-installed
ffmpeg that is only visible through a login shell's PATH would stay invisible
to it (the service-side counterpart of #62). These helpers resolve the tools
the way the remote probe does — PATH first, then the common absolute
locations — and build the PATH value a rendered unit runs with: the detected
tool directories first, then the standard system paths.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = [
    "FALLBACK_TOOL_DIRS",
    "STANDARD_PATH_DIRS",
    "resolve_tool",
    "service_path_value",
]

#: Absolute locations checked when ``shutil.which`` misses a tool; the same
#: order the remote probe falls back to. Paths are space-free by construction:
#: the probe's shell loop word-splits them.
FALLBACK_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

#: The launchd default PATH quoted in issue #71; every unit PATH carries it.
STANDARD_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def resolve_tool(
    tool: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    fallback_dirs: Sequence[str] = FALLBACK_TOOL_DIRS,
) -> str | None:
    """Absolute path of ``tool`` from PATH or the fallback directories."""
    found = which(tool)
    if found:
        return found
    for directory in fallback_dirs:
        candidate = Path(directory) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def service_path_value(
    *,
    resolve: Callable[[str], str | None] = resolve_tool,
    standard_dirs: Sequence[str] = STANDARD_PATH_DIRS,
) -> str:
    """PATH value for a rendered unit: detected tool dirs, then system dirs."""
    directories: list[str] = []
    for tool in ("ffmpeg", "ffprobe"):
        found = resolve(tool)
        if found:
            directories.append(str(Path(found).parent))
    directories.extend(standard_dirs)
    return ":".join(dict.fromkeys(directories))
