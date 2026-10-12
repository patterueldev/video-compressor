"""Install the staged project wheel in the remote venv (issue #67).

pip's plain satisfaction check compares versions only, so a rebuilt wheel of an
already-installed version was silently skipped while the install step still
reported success (the node kept running the old code). The command here
force-reinstalls the staged wheel, closing that gap for good.

The step detail is evidence, not assumption: it comes from pip's own output —
``installed <distribution>`` when pip installed the project, ``unchanged`` when
pip installed nothing.
"""

from __future__ import annotations

import re
import shlex

from videocompressor.node.models import CommandResult, InstallError
from videocompressor.node.ssh import SshRunner

__all__ = ["install_staged_wheel"]

#: pip prints this summary line when it installed packages.
_INSTALLED_MARKER = "Successfully installed"
#: pip prints this when every requirement was already satisfied (nothing installed).
_UNCHANGED_MARKER = "Requirement already satisfied"
#: The project distribution as pip names it in the summary (hyphen or underscore).
_PROJECT_DIST = re.compile(r"video[-_]compressor-\S+")


def install_staged_wheel(
    runner: SshRunner,
    *,
    venv_python: str,
    staging: str,
    host_label: str,
    timeout: float,
) -> str:
    """Force-reinstall the staged wheel; return the truthful install detail.

    ``--force-reinstall`` bypasses pip's version-only satisfaction check, so a
    rebuilt wheel of the same version still replaces the installed one.
    """
    command = (
        f"{shlex.quote(venv_python)} -m pip install --no-index"
        f" --find-links {shlex.quote(staging)} --force-reinstall video-compressor"
    )
    result = runner.run(command, timeout=timeout)
    if result.returncode != 0:
        detail = _first_line(result.stderr or result.stdout) or f"exit {result.returncode}"
        raise InstallError(f"install the worker wheel failed on {host_label}: {detail}")
    return _install_detail(result)


def _install_detail(result: CommandResult) -> str:
    """Report what pip actually did, from its own output.

    The project token is searched only after the ``Successfully installed``
    marker: pip mentions the wheel path earlier in ``Processing …`` lines, and
    the summary may wrap, but the installed names always follow the marker.
    """
    output = f"{result.stdout}\n{result.stderr}"
    marker = output.find(_INSTALLED_MARKER)
    if marker >= 0:
        match = _PROJECT_DIST.search(output, marker)
        return f"installed {match.group(0)}" if match else "installed"
    if _UNCHANGED_MARKER in output:
        return "unchanged"
    return "installed"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""
