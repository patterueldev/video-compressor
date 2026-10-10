"""PySide6 graphical client (issues #8 and #9).

PySide6 is installed only with the ``gui`` extra; this package must never be
imported by ``videocompressor.core``.

Issue #8 ships the foundation: ``app`` (entry point), ``main_window``,
``drop_zone``, ``roots_panel``, ``templates_panel``, ``clip_tables``,
``scan_runner``, plus the pure-Python ``state`` and ``scan_service``. Issue #9
adds the worker monitor on top of these.
"""
