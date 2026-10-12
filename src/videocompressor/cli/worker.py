"""``vc worker``: run the worker agent (issue #6); manage its service (issue #51).

The frozen surface (see ``.tmp/plan-51.md`` and ``docs/worker-runbook.md``)::

    vc worker --config PATH [--once]                        # foreground agent
    vc worker install-service   --config PATH [--dry-run] [--json]
    vc worker service-status    --config PATH [--json]
    vc worker uninstall-service --config PATH [--dry-run] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from videocompressor.cli.constants import EXIT_ERROR, EXIT_USAGE
from videocompressor.worker import WorkerAgent, WorkerConfigError, load_worker_config
from videocompressor.worker.client import ControllerAuthError
from videocompressor.worker.config import read_worker_id
from videocompressor.worker.service import (
    ServiceError,
    ServiceResult,
    ServiceStatus,
    default_paths,
    default_runner,
    install_service,
    service_status,
    spec_for_worker_id,
    spec_from_config,
    uninstall_service,
)
from videocompressor.worker.units import log_paths, render_unit

NAME = "worker"
HELP = "run the worker agent for this node, or manage its service"

INSTALL = "install-service"
STATUS = "service-status"
UNINSTALL = "uninstall-service"
OPERATIONS = (INSTALL, STATUS, UNINSTALL)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    parser.add_argument(
        "operation",
        nargs="?",
        choices=OPERATIONS,
        default=None,
        help=(
            "service operation: install-service (create and start), service-status "
            "(report), uninstall-service (stop and remove); omit to run the agent"
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="PATH",
        help="per-node worker config (TOML): controller URL/token, roots, scratch",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run (foreground only): claim and process at most one job, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="service operations only: print the plan without changing anything",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="service operations only: print one JSON result document on stdout",
    )


def run(args: argparse.Namespace) -> int:
    if args.operation is None:
        return _run_foreground(args)
    return _run_service(args)


def _run_foreground(args: argparse.Namespace) -> int:
    if args.dry_run or args.json:
        print(
            "vc worker: --dry-run and --json are only valid for the service operations",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        config = load_worker_config(args.config)
    except WorkerConfigError as exc:
        print(f"vc worker: {exc}", file=sys.stderr)
        return EXIT_USAGE

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    agent = WorkerAgent(config)
    restore = _install_signal_handlers(agent)
    pilot = f"; pilot root {config.pilot_root}" if config.pilot else ""
    print(
        f"vc worker: {config.worker_id} -> {config.controller_url}"
        f" (roots: {', '.join(sorted(config.roots))}; scratch: {config.scratch_dir}{pilot})"
    )
    try:
        agent.run(max_jobs=1 if args.once else None)
    except ControllerAuthError as exc:
        print(f"vc worker: controller rejected the token: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # only when the SIGINT handler could not be installed
        agent.request_stop()
    finally:
        restore()
    return 0


def _run_service(args: argparse.Namespace) -> int:
    if args.once:
        print(
            "vc worker: --once is only valid for the foreground worker",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.operation == STATUS and args.dry_run:
        print("vc worker: service-status does not accept --dry-run", file=sys.stderr)
        return EXIT_USAGE

    try:
        paths = default_paths()
        if args.operation == INSTALL:
            config = load_worker_config(args.config)
            spec = spec_from_config(config, args.config)
            return _report_result(
                install_service(spec, paths=paths, runner=default_runner, dry_run=args.dry_run),
                as_json=args.json,
            )
        spec = spec_for_worker_id(read_worker_id(args.config), args.config)
        if args.operation == STATUS:
            return _report_status(
                service_status(spec, paths=paths, runner=default_runner),
                as_json=args.json,
            )
        return _report_result(
            uninstall_service(spec, paths=paths, runner=default_runner, dry_run=args.dry_run),
            as_json=args.json,
        )
    except WorkerConfigError as exc:
        print(f"vc worker: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ServiceError as exc:
        print(f"vc worker: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _report_result(outcome: ServiceResult, *, as_json: bool) -> int:
    payload = outcome.to_dict()
    if as_json:
        print(json.dumps(payload))
        return 0
    print(f"vc worker: {payload['result']} {payload['scope']} for {payload['worker_id']}")
    print(f"vc worker: unit: {payload['unit']}")
    if payload["env_file"]:
        print(f"vc worker: env: {payload['env_file']}")
    for kind, path in log_paths(outcome.spec, outcome.paths).items():
        print(f"vc worker: log ({kind}): {path}")
    if outcome.dry_run and outcome.operation == INSTALL:
        document = render_unit(outcome.spec, outcome.paths, redact_token=True)
        print(document if isinstance(document, str) else document.decode("utf-8"), end="")
    return 0


def _report_status(status: ServiceStatus, *, as_json: bool) -> int:
    payload = status.to_dict()
    if as_json:
        print(json.dumps(payload))
        return 0
    print(f"vc worker: {payload['worker_id']} service: {payload['state']} ({payload['scope']})")
    print(f"vc worker: unit: {payload['unit']}")
    if payload["detail"]:
        print(f"vc worker: detail: {payload['detail']}")
    return 0


def _install_signal_handlers(agent: WorkerAgent) -> Callable[[], None]:
    """Install SIGTERM/SIGINT handlers that stop the agent; return a restore hook."""
    previous: dict[int, object] = {}

    def handle(signum: int, frame: object) -> None:
        agent.request_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
        except (ValueError, OSError):  # not running in the main thread
            pass

    def restore() -> None:
        for signum, old in previous.items():
            try:
                signal.signal(signum, old)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass

    return restore
