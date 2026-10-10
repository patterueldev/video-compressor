"""Archive-root registration and the worker-mapping reminder (issue #8)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.config import AppConfig, register_root, save_config
from videocompressor.core.models import ArchiveRoot
from videocompressor.core.scan import derive_root_id


def mapping_reminder(root: ArchiveRoot) -> str:
    """The reminder shown for one root: workers resolve it by id, not by path."""
    return f"Workers must map root id {root.id!r} to their own path for the same archive folder."


class RootDialog(QDialog):
    """Collect one archive root (id, name, path) with inline validation errors."""

    def __init__(self, prefill: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Register archive root")
        self._id = QLineEdit(derive_root_id(prefill))
        self._name = QLineEdit(prefill.name)
        self._path = QLineEdit(str(prefill))
        self._error = QLabel("")
        self._error.setWordWrap(True)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self._path)
        path_row.addWidget(browse)

        hint = QLabel(
            "Each worker must map this root id to its own path for the same archive folder."
        )
        hint.setWordWrap(True)

        form = QFormLayout(self)
        form.addRow("Root id", self._id)
        form.addRow("Name", self._name)
        form.addRow("This Mac's path", path_row)
        form.addRow(hint)
        form.addRow(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str, Path]:
        """The typed values: root id, name, and expanded path."""
        return self._id.text(), self._name.text(), Path(self._path.text().strip()).expanduser()

    def show_error(self, message: str) -> None:
        """Show a validation error without closing the dialog."""
        self._error.setText(message)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose archive root", self._path.text().strip()
        )
        if chosen:
            self._path.setText(chosen)


class RootsPanel(QWidget):
    """Registered roots list plus the registration flow."""

    roots_changed = Signal()

    def __init__(self, config: AppConfig, config_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.last_error: str | None = None
        self._list = QListWidget()
        register = QPushButton("Register root…")
        register.clicked.connect(lambda: self.prompt_register(Path.home()))

        title = QLabel("Archive roots (this Mac)")
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self._list)
        layout.addWidget(register)
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the list from the current config."""
        self._list.clear()
        for root in self.config.roots:
            item = QListWidgetItem(f"{root.name} ({root.id})\n{root.path}")
            item.setToolTip(mapping_reminder(root))
            self._list.addItem(item)

    def register_from_values(self, root_id: str, name: str, path: Path) -> ArchiveRoot | None:
        """Validate, persist, and list one root; ``None`` with ``last_error`` set on failure."""
        count = len(self.config.roots)
        try:
            root = register_root(self.config, root_id=root_id, name=name, path=path)
            save_config(self.config, self.config_path)
        except (ValueError, OSError) as error:
            del self.config.roots[count:]
            self.last_error = str(error)
            return None
        self.last_error = None
        self.refresh()
        self.roots_changed.emit()
        return root

    def prompt_register(self, prefill: Path) -> ArchiveRoot | None:
        """Modal registration loop; returns the new root or ``None`` when cancelled."""
        dialog = RootDialog(prefill, self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            root_id, name, path = dialog.values()
            root = self.register_from_values(root_id, name, path)
            if root is not None:
                return root
            dialog.show_error(self.last_error or "registration failed")
        return None
