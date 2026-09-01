"""The Nautilus extension and its installer.

Two modules that could hardly be more different.
:mod:`~onedriveui.ext.install` is ordinary application code. The extension
itself, :mod:`~onedriveui.ext.nautilus_onedriveui`, runs inside Nautilus and may
import **the standard library and ``gi`` only** — see its own docstring for why
importing the ``onedriveui`` package there breaks it at load with no useful
error. A test enforces that boundary with the AST.
"""

from __future__ import annotations

__all__: list[str] = []
