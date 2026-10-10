"""``vc gui`` application entry: controller session, window, and loop (issue #9).

The GUI hosts the controller in-process (plan.md) and attaches to a controller
v1 endpoint when the configured address is already served. ``gui/app.py``
remains the library-only entry from issue #8.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from videocompressor.core.config import AppConfig, default_config_path, load_config
from videocompressor.gui.controller_session import (
    ControllerAttachError,
    ControllerBindError,
    start_controller_session,
)
from videocompressor.gui.monitor_window import MonitorWindow

__all__ = ["run"]

EXIT_CONTROLLER_ERROR = 1


def run(*, config_path: Path | None = None, manifest_path: Path | None = None) -> int:
    """Start the GUI with its controller session and return the process exit code."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("Video Compressor")
    app.setOrganizationName("Video Compressor")

    try:
        config = load_config(config_path)
    except (OSError, tomllib.TOMLDecodeError) as error:
        QMessageBox.warning(
            None,
            "Video Compressor",
            f"cannot read configuration: {error}\nStarting with defaults.",
        )
        config = AppConfig()

    resolved_config = config_path if config_path is not None else default_config_path()
    try:
        session = start_controller_session(
            config, config_path=resolved_config, manifest_path=manifest_path
        )
    except (ControllerAttachError, ControllerBindError, OSError) as error:
        QMessageBox.warning(None, "Video Compressor", f"controller unavailable: {error}")
        return EXIT_CONTROLLER_ERROR

    window = MonitorWindow(
        config=config,
        config_path=resolved_config,
        manifest_path=manifest_path,
        session=session,
    )
    window.show()
    try:
        return app.exec()
    finally:
        session.close()
