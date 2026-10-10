"""Thin fixture registrar: fixture code lives in ``tests/fixtures/`` by domain.

Add a new fixture group by creating ``tests/fixtures/<domain>.py`` and adding its
dotted module path to ``pytest_plugins`` below; never grow this file.
"""

pytest_plugins = [
    "fixtures.controller",
    "fixtures.media",
    "fixtures.worker",
]
