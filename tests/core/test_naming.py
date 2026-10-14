"""Name-pattern tokens, validation, rendering, and safety (issue #87)."""

from __future__ import annotations

from datetime import datetime

import pytest

from videocompressor.core.naming import (
    DEFAULT_PATTERN,
    MAX_PATTERN_LENGTH,
    MAX_STEM_LENGTH,
    NamePatternError,
    needs_capture_time,
    render_output_name,
    validate_pattern,
)

WHEN = datetime(2025, 4, 19, 21, 17, 5)


def render(pattern: str, *, name: str = "GX010748", when: datetime | None = WHEN) -> str:
    return render_output_name(pattern, name=name, when=when)


def test_the_default_pattern_is_the_source_name():
    assert DEFAULT_PATTERN == "{name}"
    assert render("{name}") == "GX010748"
    assert validate_pattern("{name}") == []


def test_date_time_and_datetime_tokens_use_the_default_formats():
    assert render("{date}_{name}") == "20250419_GX010748"
    assert render("{time}_{name}") == "211705_GX010748"
    assert render("{datetime}_{name}") == "20250419_211705_GX010748"


def test_tokens_accept_custom_strftime_formats():
    assert render("{date:%Y%m%d}_{name}") == "20250419_GX010748"
    assert render("{time:%H%M}_{name}") == "2117_GX010748"
    assert render("{datetime:%Y%m%d%H%M}_{name}") == "202504192117_GX010748"
    assert render("{date:%Y}-{name}") == "2025-GX010748"


def test_literal_prefixes_and_suffixes_are_kept():
    assert render("trip-{name}-small") == "trip-GX010748-small"
    assert render("{date}.{time}.{name}") == "20250419.211705.GX010748"


def test_unknown_tokens_are_rejected():
    problems = validate_pattern("{camera}_{name}")
    assert len(problems) == 1 and "camera" in problems[0]
    with pytest.raises(NamePatternError, match="camera"):
        render("{camera}_{name}")


def test_a_format_on_name_is_rejected():
    assert validate_pattern("{name:%Y}") == ["the {name} token takes no format"]
    with pytest.raises(NamePatternError):
        render("{name:%Y}")


def test_invalid_strftime_formats_are_rejected():
    problems = validate_pattern("{date:%Q}")
    assert len(problems) == 1 and "%Q" in problems[0]
    with pytest.raises(NamePatternError):
        render("{datetime:%Q}{name}")
    assert validate_pattern("{date:%Y%m%d}") == []
    assert validate_pattern("{time:%-d}") == []  # GNU padding stays allowed


def test_empty_or_unbalanced_tokens_are_rejected():
    assert validate_pattern("") == ["the name pattern must not be empty"]
    assert validate_pattern("   ") == ["the name pattern must not be empty"]
    assert validate_pattern("{date:}") == ["the {date} token has an empty format"]
    assert any("unbalanced" in problem for problem in validate_pattern("{name"))
    assert any("unbalanced" in problem for problem in validate_pattern("name}"))
    assert any("unbalanced" in problem for problem in validate_pattern("{na{me}"))


def test_separators_and_control_characters_are_rejected():
    assert any("separator" in problem for problem in validate_pattern("a/{name}"))
    assert any("separator" in problem for problem in validate_pattern("a\\b{name}"))
    assert any("separator" in problem for problem in validate_pattern("{date:%Y/%m}"))
    assert any("control" in problem for problem in validate_pattern("a\x00{name}"))
    assert any("control" in problem for problem in validate_pattern("a\x1f{name}"))


def test_needs_capture_time():
    assert needs_capture_time("{name}") is False
    assert needs_capture_time("clip-{name}") is False
    assert needs_capture_time("{date}_{name}") is True
    assert needs_capture_time("{time:%H%M}") is True
    assert needs_capture_time("{datetime}") is True


def test_a_capture_time_is_required_only_for_capture_time_tokens():
    assert render("{name}", when=None) == "GX010748"
    with pytest.raises(NamePatternError, match="capture time"):
        render("{date}_{name}", when=None)
    with pytest.raises(NamePatternError, match="capture time"):
        render("{datetime:%Y%m%d%H%M}", when=None)


def test_leading_and_trailing_dots_and_spaces_are_stripped():
    assert render("  .{name}.  ") == "GX010748"
    assert render(".{name}") == "GX010748"


def test_names_that_render_empty_are_rejected():
    with pytest.raises(NamePatternError, match="empty"):
        render("...")
    with pytest.raises(NamePatternError, match="empty"):
        render(".", name=".")


def test_unsafe_rendered_names_are_refused_not_altered():
    with pytest.raises(NamePatternError, match="separator"):
        render("{name}", name="a/b")
    with pytest.raises(NamePatternError, match="separator"):
        render("{name}", name="a\\b")
    with pytest.raises(NamePatternError, match="control"):
        render("{name}", name="a\x01b")
    with pytest.raises(NamePatternError, match=r"\.\."):
        render("{name}", name="a..b")


def test_the_pattern_length_bound_is_checked():
    assert validate_pattern("x" * MAX_PATTERN_LENGTH) == []
    problems = validate_pattern("x" * (MAX_PATTERN_LENGTH + 1))
    assert len(problems) == 1 and str(MAX_PATTERN_LENGTH) in problems[0]


def test_the_rendered_stem_length_bound_is_checked():
    assert render("{name}", name="n" * MAX_STEM_LENGTH) == "n" * MAX_STEM_LENGTH
    with pytest.raises(NamePatternError, match=str(MAX_STEM_LENGTH)):
        render("{name}", name="n" * (MAX_STEM_LENGTH + 1))
