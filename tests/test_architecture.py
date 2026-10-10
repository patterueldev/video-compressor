"""Static architecture guardrails for the videocompressor package (issue #23).

Checks three things without importing the package or using pytest fixtures, so the
suite stays fast and runs without the optional ``gui`` extra or ffmpeg:

- cross-package import direction follows the table in ``docs/architecture.md``;
- PySide6 is imported only from ``videocompressor/gui/``;
- every source and test file stays within its line budget, with a shrink-only
  allowlist for the audited offenders.

Only literal import statements are inspected; dynamic imports built from strings
(``importlib.import_module("videocompressor.gui")``) are out of scope. The recorded
allowlist sizes are exact and may only move down -- the recommended first seam when
shrinking ``core/manifest.py`` is the DDL/schema plus row-codec extraction.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "videocompressor"

#: Cross-package imports allowed by docs/architecture.md. A package may always import
#: its own modules; entries list the *other* first-party packages each may import.
ALLOWED_CROSS_PACKAGE = {
    "core": frozenset(),
    "controller": frozenset({"core"}),
    "worker": frozenset({"core"}),
    "gui": frozenset({"core", "controller"}),
    "cli": frozenset({"core", "controller", "worker", "gui"}),
}
PACKAGES = frozenset(ALLOWED_CROSS_PACKAGE)

#: Physical-line budgets per area (issue #23).
SIZE_BUDGETS = {"src": 400, "tests": 450}

#: Audited current offenders; recorded sizes are exact and may only shrink.
SIZE_ALLOWLIST = {
    "src/videocompressor/core/manifest.py": 561,
    "src/videocompressor/core/scan.py": 474,
    "src/videocompressor/core/replace.py": 471,
    "src/videocompressor/core/ffmpeg.py": 470,
}


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _owning_package(path: Path) -> str:
    """Owning subpackage of a file under ``SRC`` ('' for package-root files)."""
    return ".".join(path.relative_to(SRC).parts[:-1])


def _import_targets(path: Path) -> set[str]:
    """Absolute dotted targets of every literal import in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                package = _owning_package(path)
                base = ".".join(part for part in ("videocompressor", package) if part)
                try:
                    module = importlib.util.resolve_name("." * node.level + module, base)
                except (ImportError, ValueError):
                    continue
            targets.add(module)
            targets.update(f"{module}.{alias.name}" for alias in node.names)
    return targets


def _direction_violations() -> list[str]:
    problems: list[str] = []
    for path in _python_files(SRC):
        package = _owning_package(path)
        if package not in PACKAGES:
            continue  # package-root files are outside the five documented layers
        allowed = {package} | set(ALLOWED_CROSS_PACKAGE[package])
        for target in sorted(_import_targets(path)):
            parts = target.split(".")
            if parts[0] == "videocompressor" and len(parts) > 1:
                imported = parts[1]
                if imported in PACKAGES and imported not in allowed:
                    problems.append(f"{_relative(path)}: {package} imports {target}")
    return problems


def _pyside6_violations() -> list[str]:
    problems: list[str] = []
    for path in _python_files(SRC):
        if path.relative_to(SRC).parts[0] == "gui":
            continue
        for target in sorted(_import_targets(path)):
            if target == "PySide6" or target.startswith("PySide6."):
                problems.append(f"{_relative(path)}: imports {target}")
    return problems


def test_imports_follow_package_direction() -> None:
    problems = _direction_violations()
    assert not problems, "cross-package import direction violations:\n" + "\n".join(problems)


def test_pyside6_only_under_gui() -> None:
    problems = _pyside6_violations()
    assert not problems, "PySide6 imports outside videocompressor/gui/:\n" + "\n".join(problems)


def test_files_stay_within_size_budget() -> None:
    problems: list[str] = []
    for area, budget in SIZE_BUDGETS.items():
        for path in _python_files(ROOT / area):
            relative = _relative(path)
            if relative in SIZE_ALLOWLIST:
                continue
            count = _line_count(path)
            if count > budget:
                problems.append(f"{relative}: {count} lines > {budget}; split the file")
    assert not problems, (
        "over-budget files (new offenders must be split, never allowlisted):\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize(("relative", "recorded"), sorted(SIZE_ALLOWLIST.items()))
def test_allowlisted_files_shrink_only(relative: str, recorded: int) -> None:
    count = _line_count(ROOT / relative)
    budget = SIZE_BUDGETS[relative.split("/", 1)[0]]
    assert count > budget, (
        f"{relative} is within the {budget}-line budget; remove its allowlist entry"
    )
    assert count == recorded, (
        f"{relative} has {count} lines but the allowlist records {recorded}; entries may only "
        "shrink -- lower the recorded size, or split the file and remove the entry"
    )
