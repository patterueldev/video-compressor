"""Frozen-stub tests: the interfaces exist and fail loudly until their issue lands."""

from pathlib import Path

import pytest

from videocompressor.core.probe import probe_media


def test_probe_media_is_not_implemented_yet(tmp_path: Path):
    with pytest.raises(NotImplementedError, match="issue #2"):
        probe_media(tmp_path / "clip.MP4")
