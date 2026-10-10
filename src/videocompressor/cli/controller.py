"""``vc controller``: run the local controller and worker registry (issue #7)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from videocompressor.cli.constants import EXIT_USAGE
from videocompressor.controller.server import ControllerServer, new_token, open_manifest
from videocompressor.controller.store import ControllerStore, default_db_path
from videocompressor.core.config import default_manifest_path, load_config, save_config

NAME = "controller"
HELP = "run the local controller and worker registry"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    parser.add_argument("--host", help="bind address (default: the config value, 127.0.0.1)")
    parser.add_argument("--port", type=int, help="bind port (default: the config value, 8765)")
    parser.add_argument("--config", type=Path, help="config file path (default: the per-user path)")
    parser.add_argument(
        "--db", type=Path, help="controller database path (default: the per-user path)"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="manifest database (default: the per-user path)",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print the access token (generating one if needed) and exit",
    )


def run(args: argparse.Namespace) -> int:
    if args.port is not None and not 0 <= args.port <= 65535:
        print("vc controller: --port must be between 0 and 65535", file=sys.stderr)
        return EXIT_USAGE

    config = load_config(args.config)
    if args.print_token:
        if config.controller.token is None:
            config.controller.token = new_token()
            save_config(config, args.config)
        print(config.controller.token)
        return 0

    if config.controller.token is None:
        config.controller.token = new_token()
        saved = save_config(config, args.config)
        print(f"vc controller: generated an access token and saved it to {saved}", file=sys.stderr)

    host = args.host or config.controller.host
    port = args.port if args.port is not None else config.controller.port
    store = ControllerStore.open(args.db or default_db_path())
    manifest_path = args.manifest if args.manifest is not None else default_manifest_path()
    manifest, durable = open_manifest(manifest_path)
    server = ControllerServer(
        store=store,
        manifest=manifest,
        token=config.controller.token,
        host=host,
        port=port,
        templates=config.templates,
    )
    server.start_background()
    print(f"vc controller: listening on {server.base_url}")
    if durable:
        print(f"vc controller: queue: durable manifest at {manifest_path}")
    else:
        print(
            "vc controller: queue: in-memory stub (the manifest could not be opened)"
            " - queued clips will NOT survive a restart",
            file=sys.stderr,
        )
    print("vc controller: press Ctrl-C to stop")
    try:
        server.wait()
    except KeyboardInterrupt:
        print("vc controller: stopping", file=sys.stderr)
    finally:
        server.stop()
    return 0
