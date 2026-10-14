"""Template panel: editor round-trip, create/duplicate/rename, validation (issue #8)."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6")

from videocompressor.core.config import DEFAULT_TEMPLATE_NAME, AppConfig, load_config
from videocompressor.core.models import TemplateSettings
from videocompressor.gui.templates_panel import TemplatesPanel


@pytest.fixture
def panel(tmp_path, qapp):
    config_path = tmp_path / "config.toml"
    return TemplatesPanel(AppConfig(), config_path), config_path


def test_editor_save_round_trips_to_the_config_file(panel):
    templates, config_path = panel
    edited = replace(
        TemplateSettings(),
        output_height=1080,
        video_bitrate_kbps=1200,
        timezone="UTC",
        ffmpeg_preset="slow",
    )
    templates.load_editor(edited)

    assert templates.save_current()
    assert templates.last_error is None
    loaded = load_config(config_path)
    assert loaded.templates[DEFAULT_TEMPLATE_NAME] == edited

    text = config_path.read_text(encoding="utf-8")
    assert "video_bitrate_kbps = 1200" in text
    assert "daytime_video_bitrate_kbps" not in text
    assert "evening_start" not in text


def test_save_emits_the_template_name(panel):
    templates, _path = panel
    saved: list[str] = []
    templates.template_saved.connect(saved.append)
    assert templates.save_current()
    assert saved == [DEFAULT_TEMPLATE_NAME]


def test_invalid_editor_values_block_the_write(panel):
    templates, config_path = panel
    templates.load_editor(replace(TemplateSettings(), output_height=721))

    assert not templates.save_current()
    assert templates.last_error is not None and "even" in templates.last_error
    assert not config_path.exists()


def test_invalid_timezone_blocks_the_write(panel):
    templates, config_path = panel
    templates.load_editor(replace(TemplateSettings(), timezone="Nowhere/Nowhere"))

    assert not templates.save_current()
    assert templates.last_error is not None and "timezone" in templates.last_error
    assert not config_path.exists()


# -- output name patterns (issue #87) -----------------------------------------


def test_name_pattern_save_round_trips(panel):
    templates, config_path = panel
    templates.load_editor(replace(TemplateSettings(), name_pattern="{date}_{name}"))

    assert templates.save_current()
    assert load_config(config_path).templates[DEFAULT_TEMPLATE_NAME].name_pattern == "{date}_{name}"


def test_name_pattern_hint_previews_the_render(panel):
    templates, _path = panel
    templates.load_editor(
        replace(TemplateSettings(), name_pattern="{datetime:%Y%m%d%H%M}_{name}")
    )

    assert "202504192117_GS010001.MP4" in templates.pattern_hint.text()

    templates.load_editor(replace(TemplateSettings(), name_pattern="{name}"))
    assert "GS010001.MP4" in templates.pattern_hint.text()


def test_invalid_name_pattern_blocks_the_write(panel):
    templates, config_path = panel
    templates.load_editor(replace(TemplateSettings(), name_pattern="{camera}_{name}"))

    assert not templates.save_current()
    assert templates.last_error is not None and "camera" in templates.last_error
    assert "camera" in templates.pattern_hint.text()
    assert not config_path.exists()


def test_name_pattern_survives_duplicate_and_rename(panel):
    templates, config_path = panel
    templates.load_editor(replace(TemplateSettings(), name_pattern="{date}_{name}"))
    assert templates.save_current()

    assert templates.duplicate_current("copy")
    assert templates.current_settings().name_pattern == "{date}_{name}"
    assert templates.rename_current("renamed")

    loaded = load_config(config_path)
    assert loaded.templates["renamed"].name_pattern == "{date}_{name}"


def test_create_duplicate_and_rename_persist(panel):
    templates, config_path = panel
    assert templates.create_template("vlog")
    assert templates.current_name() == "vlog"

    assert templates.duplicate_current("vlog-hd")
    assert templates.current_name() == "vlog-hd"

    assert templates.rename_current("vlog-hd-final")
    assert templates.current_name() == "vlog-hd-final"

    loaded = load_config(config_path)
    assert list(loaded.templates) == [DEFAULT_TEMPLATE_NAME, "vlog", "vlog-hd-final"]


def test_duplicate_names_are_rejected(panel):
    templates, config_path = panel
    assert not templates.create_template(DEFAULT_TEMPLATE_NAME)
    assert templates.last_error is not None and "already exists" in templates.last_error
    assert not config_path.exists()


def test_selecting_a_template_loads_and_announces_it(panel):
    templates, _path = panel
    templates.create_template("vlog")
    selected: list[str] = []
    templates.template_selected.connect(selected.append)

    assert templates.select(DEFAULT_TEMPLATE_NAME)
    assert templates.current_name() == DEFAULT_TEMPLATE_NAME
    assert selected == [DEFAULT_TEMPLATE_NAME]
    assert not templates.select("missing")
