"""``vc gui`` application entry: config load, window, and event loop (issue #8)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from videocompressor.core.config import AppConfig, default_config_path, load_config
from videocompressor.gui.main_window import MainWindow


def run(*, config_path: Path | None = None, manifest_path: Path | None = None) -> int:
    """Start the GUI and return the process exit code."""
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

    window = MainWindow(
        config=config,
        config_path=config_path if config_path is not None else default_config_path(),
        manifest_path=manifest_path,
    )
    window.show()
    return app.exec()
