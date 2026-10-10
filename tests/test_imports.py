"""The package and every engine subpackage import without the optional gui extra."""

import importlib

import pytest

MODULES = [
    "videocompressor",
    "videocompressor.cli",
    "videocompressor.core",
    "videocompressor.core.capture_time",
    "videocompressor.core.config",
    "videocompressor.core.fingerprint",
    "videocompressor.core.manifest",
    "videocompressor.core.markers",
    "videocompressor.core.models",
    "videocompressor.core.probe",
    "videocompressor.core.recover",
    "videocompressor.core.replace",
    "videocompressor.core.scan",
    "videocompressor.controller",
    "videocompressor.controller.constants",
    "videocompressor.controller.memory_manifest",
    "videocompressor.controller.server",
    "videocompressor.controller.store",
    "videocompressor.worker",
    "videocompressor.gui",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)
