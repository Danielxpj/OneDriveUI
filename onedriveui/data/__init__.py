"""Persistence package.

WP-00 froze the SQL that lives here:

  * ``schema.sql``               — the complete DDL (ARCHITECTURE §10)
  * ``migrations/001_initial.sql`` — the v1 migration, derived from it

WP-01 added the Python around it:

  * ``db.py``         — the connection factory and migration runner. WAL,
    ``synchronous=NORMAL``, ``busy_timeout=5000``, ``foreign_keys=ON``; one
    read-write connection, thread-local read-only ones, and a refusal to open a
    database under a FUSE mount.
  * ``writer.py``     — ``DbWriter``, the one thread that writes, batching on a
    100 ms timer and committing immediately for ``urgent`` work.
  * ``repo_sync.py``  — ``activity``, ``issues``, ``runs``, ``conflicts``,
    ``decisions``, ``latches``.
  * ``repo_files.py`` — ``pins``, ``cache_index``, ``versions``, ``trashbin``,
    ``share_links``, ``notifications``, ``kv``, ``folder_selection``,
    ``kfm_folder``, ``dialog_seen``.

The submodules are **not** imported here. ``writer.py`` pulls in
``PySide6.QtCore``, and ``models.py`` must stay importable with the stdlib
alone; a convenience re-export at package level would make ``import
onedriveui.data`` drag Qt into every consumer, including the Nautilus extension,
which runs inside the file manager's own process and must not.

Import what you need directly::

    from onedriveui.data import db
    from onedriveui.data.repo_files import file_states
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: Where ``schema.sql`` and ``migrations/`` live, for callers that want the DDL
#: without importing ``db`` (and therefore ``paths`` and ``errors``).
DATA_DIR: Final[Path] = Path(__file__).resolve().parent
SCHEMA_PATH: Final[Path] = DATA_DIR / "schema.sql"
MIGRATIONS_PATH: Final[Path] = DATA_DIR / "migrations"

__all__: list[str] = ["DATA_DIR", "SCHEMA_PATH", "MIGRATIONS_PATH"]
