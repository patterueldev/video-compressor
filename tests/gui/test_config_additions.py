"""Config additions for the GUI: registered roots and template management (issue #8)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from videocompressor.core.config import (
    DEFAULT_TEMPLATE_NAME,
    AppConfig,
    add_template,
    duplicate_template,
    load_config,
    register_root,
    rename_template,
    root_for_folder,
    save_config,
    validate_root_id,
    validate_template,
    validate_template_name,
)
from videocompressor.core.models import ArchiveRoot, TemplateSettings


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


@pytest.fixture
def root_dir(tmp_path: Path) -> Path:
    root = tmp_path / "share" / "CAMERA"
    root.mkdir(parents=True)
    return root


def test_register_root_round_trips(config_path, root_dir):
    config = AppConfig()
    root = register_root(config, root_id="camera", name="Camera archive", path=root_dir)
    assert root == ArchiveRoot(id="camera", name="Camera archive", path=root_dir)

    save_config(config, config_path)
    loaded = load_config(config_path)
    assert loaded.roots == [root]
    assert loaded.templates[DEFAULT_TEMPLATE_NAME] == TemplateSettings()
    assert str(root_dir) in config_path.read_text(encoding="utf-8")


def test_register_root_defaults_name_to_id(root_dir):
    config = AppConfig()
    root = register_root(config, root_id="camera", name="   ", path=root_dir)
    assert root.name == "camera"


def test_register_root_rejects_duplicates(root_dir, tmp_path):
    config = AppConfig()
    register_root(config, root_id="camera", name="Camera", path=root_dir)

    with pytest.raises(ValueError, match="already registered"):
        register_root(config, root_id="camera", name="Other", path=tmp_path)
    with pytest.raises(ValueError, match="already registered"):
        register_root(config, root_id="other", name="Other", path=root_dir)
    assert len(config.roots) == 1


def test_register_root_requires_absolute_existing_directory(tmp_path):
    config = AppConfig()
    with pytest.raises(ValueError, match="absolute"):
        register_root(config, root_id="relative", name="Relative", path=Path("share/CAMERA"))
    with pytest.raises(ValueError, match="not a directory"):
        register_root(config, root_id="missing", name="Missing", path=tmp_path / "nope")
    assert config.roots == []


@pytest.mark.parametrize("value", ["camera", "a.b-c_1", "x" * 64])
def test_validate_root_id_accepts(value):
    assert validate_root_id(value) == value


@pytest.mark.parametrize("value", ["", "bad id", "x" * 65, "caf\u00e9"])
def test_validate_root_id_rejects(value):
    with pytest.raises(ValueError):
        validate_root_id(value)


def test_root_for_folder_prefers_most_specific(config_path, tmp_path):
    archive = tmp_path / "share" / "CAMERA"
    imports = archive / "IMPORTS"
    imports.mkdir(parents=True)
    folder = imports / "2026" / "GoPro"
    folder.mkdir(parents=True)

    config = AppConfig()
    register_root(config, root_id="camera", name="Camera", path=archive)
    register_root(config, root_id="imports", name="Imports", path=imports)

    assert root_for_folder(config.roots, folder).id == "imports"
    assert root_for_folder(config.roots, archive).id == "camera"
    assert root_for_folder(config.roots, tmp_path / "other") is None


def test_root_for_folder_resolves_symlink_free_paths(root_dir):
    config = AppConfig()
    register_root(config, root_id="camera", name="Camera", path=root_dir)
    child = root_dir / "VIDEOS"
    child.mkdir()
    assert root_for_folder(config.roots, child / ".." / "VIDEOS").id == "camera"


def test_add_and_duplicate_template(config_path):
    config = AppConfig()
    vlog = add_template(config, "vlog")
    assert vlog == TemplateSettings()

    modified = replace(config.templates[DEFAULT_TEMPLATE_NAME], video_bitrate_kbps=1500)
    config.templates[DEFAULT_TEMPLATE_NAME] = modified
    copy = duplicate_template(config, DEFAULT_TEMPLATE_NAME, "copy")
    assert copy == modified
    assert list(config.templates) == [DEFAULT_TEMPLATE_NAME, "vlog", "copy"]

    save_config(config, config_path)
    loaded = load_config(config_path)
    assert loaded.templates[DEFAULT_TEMPLATE_NAME].video_bitrate_kbps == 1500
    assert loaded.templates["copy"] == modified


def test_add_template_rejects_duplicates_and_bad_names(config_path):
    config = AppConfig()
    with pytest.raises(ValueError, match="already exists"):
        add_template(config, DEFAULT_TEMPLATE_NAME)
    with pytest.raises(ValueError, match="non-empty"):
        add_template(config, " padded ")
    with pytest.raises(ValueError, match="non-empty"):
        add_template(config, "")


def test_rename_template_preserves_order_and_settings(config_path):
    config = AppConfig()
    add_template(config, "vlog")
    add_template(config, "acro")
    config.templates["vlog"] = replace(config.templates["vlog"], ffmpeg_preset="slow")

    renamed = rename_template(config, "vlog", "touring")
    assert renamed.ffmpeg_preset == "slow"
    assert list(config.templates) == [DEFAULT_TEMPLATE_NAME, "touring", "acro"]

    save_config(config, config_path)
    loaded = load_config(config_path)
    assert loaded.templates["touring"].ffmpeg_preset == "slow"
    assert set(loaded.templates) == {DEFAULT_TEMPLATE_NAME, "touring", "acro"}


def test_rename_template_rejects_collisions_and_unknown_names():
    config = AppConfig()
    add_template(config, "vlog")
    with pytest.raises(ValueError, match="already exists"):
        rename_template(config, "vlog", DEFAULT_TEMPLATE_NAME)
    with pytest.raises(ValueError, match="unknown template"):
        rename_template(config, "missing", "other")
    with pytest.raises(ValueError, match="non-empty"):
        rename_template(config, "vlog", " x")


def test_validate_template_reports_every_problem():
    broken = TemplateSettings(
        output_height=721,
        video_bitrate_kbps=0,
        audio_bitrate_kbps=0,
        audio_codec="  ",
        ffmpeg_preset="",
        timezone="Nowhere/Nowhere",
    )
    problems = validate_template(broken)
    assert len(problems) == 6
    assert any("even" in problem for problem in problems)
    assert any("Nowhere/Nowhere" in problem for problem in problems)
    assert validate_template(TemplateSettings()) == []


def test_validate_template_name_rejects_surrounding_spaces():
    assert validate_template_name("vlog") == "vlog"
    for value in ("", " vlog", "vlog "):
        with pytest.raises(ValueError):
            validate_template_name(value)


def test_missing_or_partial_config_has_no_roots(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.roots == []

    partial = tmp_path / "partial.toml"
    partial.write_text('[capture_time]\nmode = "utc"\n', encoding="utf-8")
    assert load_config(partial).roots == []


def test_hand_written_roots_table_round_trips(tmp_path, root_dir):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[[roots]]\nid = "camera"\nname = "Camera"\npath = "{root_dir}"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.roots == [ArchiveRoot(id="camera", name="Camera", path=root_dir)]


def test_roots_section_absent_from_written_config_without_roots(tmp_path):
    path = save_config(AppConfig(), tmp_path / "config.toml")
    assert "roots" not in path.read_text(encoding="utf-8")
