"""``vc worker``: run the worker agent for this node (issue #6)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from videocompressor.cli.constants import EXIT_USAGE
from videocompressor.worker import WorkerAgent, WorkerConfigError, load_worker_config
from videocompressor.worker.client import ControllerAuthError

NAME = "worker"
HELP = "run the worker agent for this node"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
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
        help="claim and process at most one job, then exit",
    )


def run(args: argparse.Namespace) -> int:
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
