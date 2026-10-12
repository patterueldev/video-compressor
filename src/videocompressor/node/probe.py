"""Remote host probe for the node bootstrap (issue #53).

One POSIX ``sh`` script reports everything as ``VC_PROBE_<key>=<value>`` lines;
the parser is key-agnostic, so unknown or missing markers never crash the probe.
The probe is read-only and runs before any install decision. It prepends the
Homebrew locations to ``PATH`` because non-interactive SSH shells on macOS
often miss them.
"""

from __future__ import annotations

import shlex

from videocompressor.node.models import NodeProbe, ProbeError
from videocompressor.node.ssh import SshRunner

__all__ = ["PROBE_TIMEOUT", "parse_probe_output", "probe_node"]

PROBE_TIMEOUT = 30.0

_PROBE_SCRIPT = """\
set -u
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
expand() {
    case "$1" in
        "~") printf '%s' "$HOME" ;;
        "~/"*) printf '%s' "$HOME/${1#~/}" ;;
        *) printf '%s' "$1" ;;
    esac
}
mark() { printf 'VC_PROBE_%s=%s\\n' "$1" "$2"; }
install_dir=$(expand "${VC_INSTALL_DIR:-}")
scratch_dir=$(expand "${VC_SCRATCH_DIR:-}")
mark version 1
mark platform "$(uname -s 2>/dev/null || true)"
mark home "$HOME"
mark hostname "$(hostname 2>/dev/null || true)"
mark install_dir "$install_dir"
if command -v python3 >/dev/null 2>&1; then
    mark python_bin "$(command -v python3)"
    mark python_version "$(python3 --version 2>&1 | head -n 1)"
    if python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
        mark venv_ok yes
    else
        mark venv_ok no
    fi
fi
if command -v ffmpeg >/dev/null 2>&1; then
    mark ffmpeg "$(command -v ffmpeg)"
    mark ffmpeg_version "$(ffmpeg -version 2>/dev/null | head -n 1)"
fi
if command -v ffprobe >/dev/null 2>&1; then
    mark ffprobe "$(command -v ffprobe)"
fi
mark disk_free_kb "$(df -Pk "$HOME" 2>/dev/null | tail -n 1 | awk '{print $4}')"
if [ -n "$scratch_dir" ]; then
    if [ -d "$scratch_dir" ]; then
        scratch_df="$scratch_dir"
    else
        scratch_df=$(dirname "$scratch_dir")
    fi
    mark scratch_free_kb "$(df -Pk "$scratch_df" 2>/dev/null | tail -n 1 | awk '{print $4}')"
fi
if [ -n "$install_dir" ]; then
    if [ -x "$install_dir/venv/bin/vc" ]; then
        mark venv_present yes
        mark worker_version "$("$install_dir/venv/bin/vc" --version 2>/dev/null | head -n 1)"
    else
        mark venv_present no
    fi
    if [ -f "$install_dir/worker.toml" ]; then
        mark config_present yes
    else
        mark config_present no
    fi
fi
"""


def parse_probe_output(text: str) -> dict[str, str]:
    """Parse ``VC_PROBE_<key>=<value>`` marker lines; ignore everything else."""
    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("VC_PROBE_") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key[len("VC_PROBE_"):]] = value
    return data


def _probe_command(install_dir: str, scratch_dir: str | None) -> str:
    env = {"VC_INSTALL_DIR": install_dir}
    if scratch_dir:
        env["VC_SCRATCH_DIR"] = scratch_dir
    prefix = " ".join(f"{name}={shlex.quote(value)}" for name, value in env.items())
    return f"{prefix} sh -c {shlex.quote(_PROBE_SCRIPT)}"


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def probe_node(
    runner: SshRunner,
    *,
    install_dir: str = "~/.videocompressor",
    scratch_dir: str | None = None,
    timeout: float = PROBE_TIMEOUT,
) -> NodeProbe:
    """Run the read-only probe and assemble a :class:`NodeProbe`."""
    command = _probe_command(install_dir, scratch_dir)
    result = runner.run(command, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ProbeError(f"remote probe failed: {detail}")
    data = parse_probe_output(result.stdout)
    if data.get("version") != "1":
        raise ProbeError(
            "remote probe output is missing the VC_PROBE markers "
            "(is the remote shell POSIX-compatible?)"
        )
    return NodeProbe(
        platform=data.get("platform", ""),
        home=data.get("home", ""),
        hostname=data.get("hostname") or None,
        install_dir=data.get("install_dir", ""),
        python_bin=data.get("python_bin") or None,
        python_version=data.get("python_version") or None,
        venv_ok=data.get("venv_ok") == "yes",
        ffmpeg=data.get("ffmpeg") or None,
        ffprobe=data.get("ffprobe") or None,
        ffmpeg_version=data.get("ffmpeg_version") or None,
        disk_free_kb=_int_or_none(data.get("disk_free_kb")),
        scratch_free_kb=_int_or_none(data.get("scratch_free_kb")),
        venv_present=data.get("venv_present") == "yes",
        config_present=data.get("config_present") == "yes",
        worker_version=data.get("worker_version") or None,
        service_status=None,
        service_ok=None,
    )
