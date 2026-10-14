"""Per-template output-name patterns (issue #87).

A template carries a *name pattern* that decides the output file name for
output-folder destinations. A pattern is literal text plus capture-time tokens;
the default ``{name}`` keeps the source clip's own name.

Tokens:

| Token | Default format | Renders |
| --- | --- | --- |
| ``{name}`` | — | the source file name without its suffix |
| ``{date}`` | ``%Y%m%d`` | capture date |
| ``{time}`` | ``%H%M%S`` | capture time |
| ``{datetime}`` | ``%Y%m%d_%H%M%S`` | capture date and time |

Every date/time token accepts an optional ``:<strftime>`` format inside the
braces (``{date:%Y%m%d}``, ``{time:%H%M}``, ``{datetime:%Y%m%d%H%M}``);
``{name}`` takes no format. Literal text is allowed anywhere.

Examples for source ``GX010748.MP4`` captured 2025-04-19 21:17::

    {name}                          -> GX010748
    {date}_{name}                   -> 20250419_GX010748
    {datetime:%Y%m%d%H%M}_{name}    -> 202504192117_GX010748

The module is pure Python (no Qt): the GUI previews and the worker render the
same pattern with the same rules.
"""

from __future__ import annotations

import re
from datetime import datetime

__all__ = [
    "DEFAULT_PATTERN",
    "MAX_PATTERN_LENGTH",
    "MAX_STEM_LENGTH",
    "NamePatternError",
    "needs_capture_time",
    "render_output_name",
    "validate_pattern",
]

#: The pre-#87 behavior: outputs keep the source clip's own file name.
DEFAULT_PATTERN = "{name}"

#: Longest accepted pattern text, before any token is rendered.
MAX_PATTERN_LENGTH = 200

#: Longest accepted rendered stem (the file name without its suffix).
MAX_STEM_LENGTH = 160

#: strftime formats applied when a date/time token carries no ``:format``.
DEFAULT_FORMATS: dict[str, str] = {
    "date": "%Y%m%d",
    "time": "%H%M%S",
    "datetime": "%Y%m%d_%H%M%S",
}

_TOKEN_RE = re.compile(r"\{([^{}]*)\}")

#: Directives accepted after ``%`` in a strftime format: the C89/POSIX set,
#: the ISO extensions, and the common GNU padding/colon forms (``%-d``,
#: ``%:z``, ``%0e``). Modifiers ``E``/``O`` (``%Ey``, ``%Om``) are allowed.
_STRFTIME_LETTERS = frozenset("aAbBcCdDeEFgGhHIjklmMnOpPrRsStTuUVwWxXyYzZ%")
_STRFTIME_MODIFIERS = frozenset("EO-_0:")

#: Fixed instant used to probe a token's strftime format during validation.
_PROBE_WHEN = datetime(2025, 4, 19, 21, 17, 5)


class NamePatternError(ValueError):
    """A name pattern cannot be used (invalid, needs a capture time, or unsafe)."""


def needs_capture_time(pattern: str) -> bool:
    """Whether ``pattern`` uses a capture-time token (``{date}``/``{time}``/``{datetime}``)."""
    for match in _TOKEN_RE.finditer(pattern):
        token = match.group(1).partition(":")[0]
        if token in DEFAULT_FORMATS:
            return True
    return False


def validate_pattern(pattern: str) -> list[str]:
    """Return human-readable problems with ``pattern`` (empty when valid).

    Rejects empty/whitespace-only patterns, unknown tokens, a format on
    ``{name}``, invalid strftime formats, unbalanced braces, path separators,
    control characters, and patterns over :data:`MAX_PATTERN_LENGTH`.
    """
    if not pattern.strip():
        return ["the name pattern must not be empty"]
    problems: list[str] = []
    if len(pattern) > MAX_PATTERN_LENGTH:
        problems.append(f"the name pattern must be at most {MAX_PATTERN_LENGTH} characters")
    if "/" in pattern or "\\" in pattern:
        problems.append("the name pattern must not contain path separators")
    if any(ord(char) < 32 or ord(char) == 127 for char in pattern):
        problems.append("the name pattern must not contain control characters")
    remainder = _TOKEN_RE.sub("", pattern)
    if "{" in remainder or "}" in remainder:
        problems.append("the name pattern must not contain unbalanced braces")
    for match in _TOKEN_RE.finditer(pattern):
        problems.extend(_token_problems(match.group(1)))
    return problems


def render_output_name(pattern: str, *, name: str, when: datetime | None) -> str:
    """Render ``pattern`` into a safe output stem (the name without its suffix).

    ``name`` is the source clip's stem; ``when`` is the capture time, required
    only when the pattern carries a capture-time token. Raises
    :class:`NamePatternError` for an invalid pattern, for a pattern that needs
    a capture time when ``when`` is ``None``, or for an unsafe/empty result.
    The result is a single path component: leading and trailing dots and spaces
    are stripped, and separators, ``..``, and control characters are refused
    rather than silently altered.
    """
    problems = validate_pattern(pattern)
    if problems:
        raise NamePatternError("; ".join(problems))
    if when is None and needs_capture_time(pattern):
        raise NamePatternError("the name pattern needs a capture time, but none is available")
    stem = _sanitize(_render(pattern, name=name, when=when))
    if len(stem) > MAX_STEM_LENGTH:
        raise NamePatternError(
            f"the rendered name is longer than {MAX_STEM_LENGTH} characters"
        )
    return stem


# -- internals -------------------------------------------------------------


def _render(pattern: str, *, name: str, when: datetime | None) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        token, separator, fmt = body.partition(":")
        if token == "name":
            return name
        if when is None:  # unreachable: needs_capture_time was checked above
            raise NamePatternError(
                "the name pattern needs a capture time, but none is available"
            )
        return when.strftime(fmt if separator else DEFAULT_FORMATS[token])

    return _TOKEN_RE.sub(replace, pattern)


def _token_problems(body: str) -> list[str]:
    token, separator, fmt = body.partition(":")
    if token == "name":
        if separator:
            return ["the {name} token takes no format"]
        return []
    if token not in DEFAULT_FORMATS:
        return [f"unknown token {body!r}"]
    if separator and not fmt:
        return [f"the {{{token}}} token has an empty format"]
    effective = fmt if separator else DEFAULT_FORMATS[token]
    if not _format_renders(effective):
        return [f"invalid strftime format {fmt!r} for the {{{token}}} token"]
    return []


def _format_renders(fmt: str) -> bool:
    """Whether ``fmt`` is a usable strftime format.

    The probe render catches platforms that report an invalid directive with
    ``ValueError``; the directive scan makes the verdict portable, because
    lenient C libraries (macOS and glibc) copy unknown directives through as
    literal text instead of failing.
    """
    try:
        _PROBE_WHEN.strftime(fmt)
    except ValueError:
        return False
    return _directives_well_formed(fmt)


def _directives_well_formed(fmt: str) -> bool:
    position = 0
    length = len(fmt)
    while position < length:
        if fmt[position] != "%":
            position += 1
            continue
        position += 1
        if position < length and fmt[position] in _STRFTIME_MODIFIERS:
            position += 1
        if position >= length or fmt[position] not in _STRFTIME_LETTERS:
            return False
        position += 1
    return True


def _sanitize(rendered: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in rendered):
        raise NamePatternError("the rendered name contains control characters")
    if "/" in rendered or "\\" in rendered:
        raise NamePatternError("the rendered name contains path separators")
    stem = rendered.strip(" .")
    if ".." in stem:
        raise NamePatternError("the rendered name must not contain '..'")
    if not stem:
        raise NamePatternError("the rendered name is empty")
    return stem
