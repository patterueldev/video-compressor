"""OpenSSH transport seam for the remote node bootstrap (issue #53).

Remote work goes through :class:`SshRunner`; the real implementation shells out
to the system ``ssh``/``scp`` binaries, so there is no new dependency and the
operator's ``~/.ssh`` config and agent apply. Tests inject a fake runner, and
argument construction is exercised against a shim executable without network.

Auth is key-based only: ``BatchMode=yes`` fails fast instead of prompting.
Host keys default to ``accept-new`` (first contact is pinned, later changes
fail); pass ``strict_host_keys=True`` to require a pre-seeded known_hosts.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Protocol

from videocompressor.node.models import (
    CommandResult,
    RemoteHost,
    SshError,
    SshTimeoutError,
)

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "OpenSSHRunner",
    "SshRunner",
    "UPLOAD_TIMEOUT",
    "build_scp_options",
    "build_ssh_argv",
    "build_ssh_options",
    "make_runner",
]

#: Timeout for a plain remote command (probe, checks, service operations).
DEFAULT_COMMAND_TIMEOUT = 30.0

#: Timeout for streaming a file or directory to the remote host.
UPLOAD_TIMEOUT = 300.0


class SshRunner(Protocol):
    """The remote-execution seam; faked in every unit test."""

    def run(self, command: str, *, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> CommandResult: ...

    def write_text(self, remote_path: str, text: str) -> None: ...

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None: ...


def build_ssh_options(host: RemoteHost) -> list[str]:
    """Common ``ssh`` options: no prompts, bounded connect, host-key policy."""
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, round(host.connect_timeout))}",
        "-o",
        f"StrictHostKeyChecking={'yes' if host.strict_host_keys else 'accept-new'}",
    ]
    if host.port is not None:
        options += ["-p", str(host.port)]
    if host.identity:
        options += ["-i", str(host.identity)]
    return options


def build_scp_options(host: RemoteHost) -> list[str]:
    """The same policy for ``scp`` (which uses ``-P`` for the port)."""
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, round(host.connect_timeout))}",
        "-o",
        f"StrictHostKeyChecking={'yes' if host.strict_host_keys else 'accept-new'}",
    ]
    if host.port is not None:
        options += ["-P", str(host.port)]
    if host.identity:
        options += ["-i", str(host.identity)]
    return options


def build_ssh_argv(host: RemoteHost, command: str, *, ssh_bin: str = "ssh") -> list[str]:
    """Build the full ``ssh`` argv for one remote command."""
    return [ssh_bin, *build_ssh_options(host), host.destination(), command]


def build_scp_argv(
    host: RemoteHost,
    source: str,
    destination: str,
    *,
    recursive: bool = False,
    scp_bin: str = "scp",
) -> list[str]:
    """Build the full ``scp`` argv for one upload."""
    argv = [scp_bin, *build_scp_options(host)]
    if recursive:
        argv.append("-r")
    argv += [source, destination]
    return argv


class OpenSSHRunner:
    """Run remote commands through the system OpenSSH binaries."""

    def __init__(self, host: RemoteHost, *, ssh_bin: str = "ssh", scp_bin: str = "scp") -> None:
        self.host = host
        self.ssh_bin = ssh_bin
        self.scp_bin = scp_bin

    def run(self, command: str, *, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> CommandResult:
        argv = build_ssh_argv(self.host, command, ssh_bin=self.ssh_bin)
        return self._invoke(argv, command=command, timeout=timeout, label="ssh")

    def write_text(self, remote_path: str, text: str) -> None:
        """Write ``text`` to ``remote_path`` atomically with mode 0600.

        The content travels on stdin, so the token never appears in a local
        temporary file or a process argument list.
        """
        tmp = f"{remote_path}.vc-tmp"
        command = (
            f"umask 077 && cat > {shlex.quote(tmp)}"
            f" && chmod 600 {shlex.quote(tmp)}"
            f" && mv {shlex.quote(tmp)} {shlex.quote(remote_path)}"
        )
        argv = build_ssh_argv(self.host, command, ssh_bin=self.ssh_bin)
        result = self._invoke(argv, command=command, timeout=UPLOAD_TIMEOUT, input_text=text,
                              label="ssh")
        if result.returncode != 0:
            raise SshError(
                f"could not write {remote_path} on {self.host.destination()}: "
                f"{result.stderr.strip() or f'exit {result.returncode}'}",
                stderr=result.stderr,
            )

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        """Copy the contents of ``local_dir`` into ``remote_dir``."""
        source = f"{local_dir.expanduser().resolve()}/."
        argv = build_scp_argv(
            self.host,
            source,
            f"{self.host.destination()}:{remote_dir}",
            recursive=True,
            scp_bin=self.scp_bin,
        )
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=UPLOAD_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise SshTimeoutError(
                f"scp to {self.host.destination()} timed out after {UPLOAD_TIMEOUT:g}s"
            ) from exc
        except OSError as exc:
            raise SshError(f"cannot run {argv[0]}: {exc}") from exc
        if proc.returncode != 0:
            raise SshError(
                f"scp to {self.host.destination()} failed: "
                f"{(proc.stderr or '').strip() or f'exit {proc.returncode}'}",
                stderr=proc.stderr or "",
            )

    def _invoke(
        self,
        argv: list[str],
        *,
        command: str,
        timeout: float,
        input_text: str | None = None,
        label: str,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                argv, input=input_text, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise SshTimeoutError(
                f"{label} to {self.host.destination()} timed out after {timeout:g}s: {command}"
            ) from exc
        except OSError as exc:
            raise SshError(f"cannot run {argv[0]}: {exc}") from exc
        if proc.returncode == 255:
            detail = (proc.stderr or "").strip() or "connection failed"
            raise SshError(f"{label} to {self.host.destination()} failed: {detail}",
                           stderr=proc.stderr or "")
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


def make_runner(host: RemoteHost) -> SshRunner:
    """Build the real runner; tests monkeypatch this factory."""
    return OpenSSHRunner(host)
