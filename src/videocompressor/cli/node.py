"""``vc node``: bootstrap and manage a worker on a remote host over SSH (issue #53).

Every action is programmatic: probe reports before changes, install ships a
wheelhouse, pushes the worker config, and drives the remote service through the
frozen #51 surface. Exit codes: ``0`` success, ``2`` usage, ``1`` runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from videocompressor.cli.constants import EXIT_ERROR, EXIT_USAGE
from videocompressor.node import (
    DEFAULT_INSTALL_DIR,
    NodeBootstrap,
    NodeError,
    RemoteHost,
    StepReport,
    WorkerSpec,
)
from videocompressor.node.ssh import make_runner
from videocompressor.worker.config import DEFAULT_THREAD_CAP

NAME = "node"
HELP = "manage a worker on a remote host over ssh (issue #53)"


class _UsageError(ValueError):
    """Invalid CLI input that is not an argparse failure."""


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    actions = parser.add_subparsers(dest="node_action", metavar="ACTION")

    probe = actions.add_parser(
        "probe", help="report the remote platform, worker, and service state"
    )
    _add_connection_arguments(probe)
    _add_install_dir_argument(probe)
    probe.add_argument(
        "--scratch-dir", metavar="PATH", help="also report free space for this remote path"
    )
    probe.add_argument("--json", action="store_true", help="print the probe as JSON")

    install = actions.add_parser("install", help="install or update the worker and its service")
    _add_connection_arguments(install)
    _add_install_dir_argument(install)
    install.add_argument(
        "--controller-url",
        required=True,
        metavar="URL",
        help="controller URL reachable from the remote host (no path)",
    )
    token = install.add_mutually_exclusive_group(required=True)
    token.add_argument(
        "--token-file", type=Path, metavar="PATH",
        help="local file holding the controller token",
    )
    token.add_argument(
        "--token-env", metavar="VAR",
        help="local environment variable holding the controller token",
    )
    install.add_argument(
        "--root",
        action="append",
        default=[],
        required=True,
        metavar="ID=PATH",
        help="controller root id mapped to a remote path (repeatable)",
    )
    install.add_argument(
        "--scratch-dir", required=True, metavar="PATH",
        help="scratch directory on the remote host",
    )
    install.add_argument(
        "--thread-cap", type=int, default=DEFAULT_THREAD_CAP, metavar="N",
        help=f"FFmpeg threads on the remote host (default {DEFAULT_THREAD_CAP})",
    )
    install.add_argument(
        "--worker-id", metavar="ID", help="worker id (default: the remote hostname)"
    )
    install.add_argument("--name", metavar="NAME", help="worker display name")
    install.add_argument(
        "--python", default="python3", metavar="PATH",
        help="remote Python interpreter (default: python3)",
    )
    install.add_argument(
        "--wheelhouse", type=Path, metavar="DIR",
        help="local directory with prebuilt wheels (default: build one)",
    )
    install.add_argument(
        "--source-dir", type=Path, default=Path("."), metavar="DIR",
        help="project checkout used to build the wheelhouse (default: .)",
    )
    install.add_argument("--json", action="store_true", help="print the report as JSON")

    status = actions.add_parser("status", help="report the installed worker and service state")
    _add_connection_arguments(status)
    _add_install_dir_argument(status)
    status.add_argument("--json", action="store_true", help="print the report as JSON")

    uninstall = actions.add_parser(
        "uninstall", help="stop the service and remove the worker config"
    )
    _add_connection_arguments(uninstall)
    _add_install_dir_argument(uninstall)
    uninstall.add_argument(
        "--purge", action="store_true", help="also remove the managed install directory"
    )
    uninstall.add_argument("--json", action="store_true", help="print the report as JSON")


def _add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="remote host name or IP")
    parser.add_argument("--user", help="SSH user (default: ssh config / current user)")
    parser.add_argument("--port", type=int, help="SSH port")
    parser.add_argument("--identity", metavar="PATH", help="private key file")
    parser.add_argument(
        "--strict-host-keys",
        action="store_true",
        help="require a known host key (default: accept-new on first contact)",
    )
    parser.add_argument(
        "--connect-timeout", type=float, default=10.0, metavar="SECONDS",
        help="SSH connect timeout (default 10)",
    )


def _add_install_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--install-dir", default=DEFAULT_INSTALL_DIR, metavar="PATH",
        help=f"managed directory on the remote host (default {DEFAULT_INSTALL_DIR})",
    )


def run(args: argparse.Namespace) -> int:
    action = getattr(args, "node_action", None)
    if action is None:
        print("vc node: choose an action (see 'vc node --help')", file=sys.stderr)
        return EXIT_USAGE

    host = RemoteHost(
        host=args.host,
        user=args.user,
        port=args.port,
        identity=args.identity,
        strict_host_keys=args.strict_host_keys,
        connect_timeout=args.connect_timeout,
    )
    bootstrap = NodeBootstrap(
        make_runner(host),
        host,
        install_dir=args.install_dir,
        python_bin=getattr(args, "python", "python3"),
        source_dir=getattr(args, "source_dir", Path(".")),
        wheelhouse=getattr(args, "wheelhouse", None),
    )
    try:
        if action == "probe":
            return _probe(args, host, bootstrap)
        if action == "install":
            return _install(args, host, bootstrap)
        if action == "status":
            return _status(args, host, bootstrap)
        return _uninstall(args, host, bootstrap)
    except _UsageError as exc:
        print(f"vc node: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except NodeError as exc:
        print(f"vc node: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _probe(args: argparse.Namespace, host: RemoteHost, bootstrap: NodeBootstrap) -> int:
    probe = bootstrap.probe(scratch_dir=args.scratch_dir)
    if args.json:
        print(json.dumps(probe.to_dict(), indent=2))
        return 0
    print(f"vc node: probe {host.destination()}")
    print(f"  platform: {probe.platform or 'unknown'}")
    print(f"  python: {probe.python_version or 'not found'}")
    print(f"  venv: {'available' if probe.venv_ok else 'unavailable'}")
    print(f"  ffmpeg: {probe.ffmpeg or 'not found'}")
    print(f"  worker: {probe.worker_version or 'not installed'}")
    if probe.service_status is not None:
        state = "running" if probe.service_ok else "not running / unknown"
        print(f"  service: {state} ({probe.service_status})")
    else:
        print("  service: not present")
    if probe.disk_free_kb is not None:
        print(f"  disk free: {probe.disk_free_kb} kB (home)")
    return 0


def _install(args: argparse.Namespace, host: RemoteHost, bootstrap: NodeBootstrap) -> int:
    spec = _build_spec(args)
    report = bootstrap.install(spec)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_steps(report.steps)
        _print_warnings(report.warnings)
        if report.probe.service_ok:
            service, outcome = "running", "ready"
        else:
            service = report.probe.service_status or "state unknown"
            outcome = "installed"
        print(f"vc node: worker {report.worker_id} {outcome} on {host.destination()}"
              f" (service: {service})")
    return 0 if report.ok else EXIT_ERROR


def _status(args: argparse.Namespace, host: RemoteHost, bootstrap: NodeBootstrap) -> int:
    report = bootstrap.status()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    probe = report.probe
    print(f"vc node: status {host.destination()}")
    print(f"  worker: {probe.worker_version or 'not installed'}")
    print(f"  config: {'present' if probe.config_present else 'missing'}")
    for step in report.steps:
        print(f"  {step.step}: {step.detail}")
    return 0


def _uninstall(args: argparse.Namespace, host: RemoteHost, bootstrap: NodeBootstrap) -> int:
    report = bootstrap.uninstall(purge=args.purge)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_steps(report.steps)
        _print_warnings(report.warnings)
        print(f"vc node: uninstall complete on {host.destination()}")
    return 0 if report.ok else EXIT_ERROR


def _build_spec(args: argparse.Namespace) -> WorkerSpec:
    return WorkerSpec(
        controller_url=args.controller_url,
        token=_read_token(args),
        roots=_parse_roots(args.root),
        scratch_dir=args.scratch_dir,
        worker_id=args.worker_id,
        name=args.name,
        thread_cap=args.thread_cap,
    )


def _read_token(args: argparse.Namespace) -> str:
    if args.token_file is not None:
        try:
            token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise _UsageError(f"cannot read --token-file {args.token_file}: {exc}") from exc
    else:
        token = os.environ.get(args.token_env, "").strip()
        if not token:
            raise _UsageError(f"environment variable {args.token_env} is not set or empty")
    if not token:
        raise _UsageError("the controller token is empty")
    return token


def _parse_roots(values: list[str]) -> dict[str, str]:
    roots: dict[str, str] = {}
    for value in values:
        root_id, separator, path = value.partition("=")
        if not separator or not root_id or not path:
            raise _UsageError(f"--root expects ID=PATH, got {value!r}")
        if root_id in roots:
            raise _UsageError(f"duplicate --root id {root_id!r}")
        roots[root_id] = path
    if not roots:
        raise _UsageError("at least one --root ID=PATH mapping is required")
    return roots


def _print_steps(steps: list[StepReport]) -> None:
    for step in steps:
        status = "ok" if step.ok else "failed"
        detail = f" - {step.detail}" if step.detail else ""
        print(f"vc node: {step.step}: {status}{detail}")


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"vc node: warning: {warning}", file=sys.stderr)
