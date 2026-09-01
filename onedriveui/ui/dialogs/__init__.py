"""Every modal this application shows, and the rule they all follow.

**The safe answer is the default.** A dialog that asks whether to delete four
thousand files has "Restore files" as its primary button, not "Delete them".
Windows does this too, and the reason is the same on both: the primary button is
what gets pressed by muscle memory, by Return, and by someone who has stopped
reading — so it must be the answer that can be undone.

The three groups:

* :mod:`~onedriveui.ui.dialogs.base` — the shared shape, and
  :func:`~onedriveui.ui.dialogs.base.unavailable` for the controls that cannot
  work on Linux and are shown **disabled with a reason** rather than hidden.
* :mod:`~onedriveui.ui.dialogs.sync_dialogs` — the destructive gates: mass
  delete, first delete, resync, unlink, reset.
* :mod:`~onedriveui.ui.dialogs.file_dialogs` — the per-file ones: free up
  space, download all, share, version history, conflicts.
* :mod:`~onedriveui.ui.dialogs.misc_dialogs` — quit, folder choice, vault.
"""

from __future__ import annotations

from onedriveui.ui.dialogs.base import (
    BaseDialog,
    DialogResult,
    disable_with_reason,
    remember_choice,
    unavailable,
)

__all__ = [
    "BaseDialog", "DialogResult", "disable_with_reason", "remember_choice",
    "unavailable",
]
