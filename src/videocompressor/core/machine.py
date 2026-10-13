"""Stable machine identity shared by the worker agent and the GUI (issue #80).

The worker registration carries this value so the app can tell a remote node
that happens to mount the share at the same paths from the machine it runs on
(:mod:`videocompressor.gui.local_worker_duplicate`). The identity is the
hostname: both sides on one machine read the identical value, and the value
only ever travels to the operator's own controller. It is compared exactly —
no case folding, so two genuinely distinct machines whose names differ only by
case never collapse into one.
"""

from __future__ import annotations

import socket

__all__ = ["machine_identity"]


def machine_identity() -> str | None:
    """The local machine's identity (its hostname), or ``None`` when unknown."""
    try:
        name = socket.gethostname().strip()
    except OSError:
        return None
    return name or None
