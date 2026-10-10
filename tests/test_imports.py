"""The package and every engine subpackage import without the optional gui extra."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import videocompressor


def _on_walk_error(name: str) -> None:
    """Tolerate only the gui package, whose import may need the optional extra.

    ``pkgutil.walk_packages`` imports subpackages to recurse into them. The gui
    package is still yielded before that import, so the parametrized case below
    accounts for it separately; every other package must import cleanly.
    """
    if name != "videocompressor.gui":
        raise RuntimeError(f"importing {name} failed during module discovery")


MODULES = ["videocompressor"] + sorted(
    info.name
    for info in pkgutil.walk_packages(
        videocompressor.__path__,
        prefix="videocompressor.",
        onerror=_on_walk_error,
    )
)


def _missing_pyside(exc: ModuleNotFoundError) -> bool:
    return (exc.name or "").partition(".")[0] == "PySide6"


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if module == "videocompressor.gui" and _missing_pyside(exc):
            pytest.skip("videocompressor.gui needs the optional gui extra (PySide6)")
        raise
