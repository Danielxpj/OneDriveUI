"""The shape every dialog shares, and the "nothing is silently missing" rule.

Two things live here.

**:class:`BaseDialog`** wraps the widget kit's ``ContentDialog`` with the pieces
every modal in this application needs: a result that says what the user chose
rather than just accepted/rejected, an optional "don't show this again"
checkbox that persists, and — for the destructive ones — a **safe default**.

**:func:`unavailable`** is the other half of a principle that runs through the
whole UI: *a control that cannot work on Linux is shown disabled with its
reason, never hidden.* Hiding it makes the user hunt for a feature they know
exists and conclude the client is incomplete. Disabling it with one sentence —
"rclone cannot revoke a OneDrive sharing link. Use the OneDrive website to stop
sharing." — tells them what is true and where to go instead. The difference
between those two experiences is most of what makes a third-party client feel
trustworthy rather than half-finished.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from onedriveui.models import DialogKey
from onedriveui.strings import DIALOG
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.containers import ContentDialog

log = logging.getLogger(__name__)

__all__ = ["BaseDialog", "DialogResult", "unavailable", "disable_with_reason",
           "remember_choice", "was_dismissed"]


class DialogResult(StrEnum):
    """What the user actually chose.

    Richer than accepted/rejected on purpose. "Delete them" and "Restore files"
    are both a completed dialog, and the caller has to be able to tell them
    apart without knowing which button happened to be primary.
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"
    DISMISSED = "dismissed"


def unavailable(reason: str) -> str:
    """Prefix a reason with "Not available on Linux: ".

    Args:
        reason: Why, in one sentence, from ``strings``.

    Returns:
        The full sentence shown beside a disabled control.
    """
    return f"{DIALOG.UNAVAILABLE_PREFIX}{reason}"


def disable_with_reason(widget: QWidget, reason: str) -> QWidget:
    """Disable a control and attach its reason. **Never hide it.**

    Args:
        widget: The control.
        reason: One sentence, from ``strings``.

    Returns:
        The widget, for chaining.

    Hiding it would make a user who knows the feature exists hunt for it and
    conclude this client is missing something. Disabling it with an explanation
    says what is true and, usually, where the thing they want does work. That
    difference is most of what separates a third-party client that feels
    trustworthy from one that feels half-finished.
    """
    widget.setEnabled(False)
    widget.setToolTip(reason)
    widget.setAccessibleDescription(reason)
    return widget


def remember_choice(key: DialogKey) -> None:
    """Record that the user ticked "don't show this again"."""
    from onedriveui.data import repo_files

    try:
        repo_files.mark_dialog_seen(key)
    except Exception:  # noqa: BLE001 - a forgotten preference is not fatal
        log.debug("could not record the %s dialog choice", key.value,
                  exc_info=True)


def was_dismissed(key: DialogKey) -> bool:
    """Has the user asked not to see this dialog again?"""
    from onedriveui.data import repo_files

    try:
        return bool(repo_files.dialog_seen(key))
    except Exception:  # noqa: BLE001
        return False


@dataclass(slots=True)
class DialogSpec:
    """Everything a modal needs, as data.

    As data rather than as constructor arguments so a test can assert the
    *shape* of a dialog — which button is primary, whether it can be dismissed —
    without constructing a widget, and so the safe-default rule below can be
    checked by inspection.
    """

    title: str
    body: str = ""
    primary: str = ""
    secondary: str = ""
    close: str = ""
    footnote: str = ""
    remember: DialogKey | None = None
    #: False for the ones that must be answered. A mass delete that could be
    #: dismissed with Escape would resolve to "the files were deleted" by
    #: default, which is the one outcome the dialog exists to prevent.
    dismissible: bool = True


class BaseDialog(ContentDialog):
    """A Fluent modal with a result, an optional footnote and a "don't ask" box.

    Args:
        spec: What to show.
        parent: Qt parent.
    """

    def __init__(self, spec: DialogSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent, title=spec.title, body=spec.body)
        self.spec = spec
        self.result_choice = DialogResult.DISMISSED
        self._remember_box: QCheckBox | None = None

        extra = self._build_extra(spec)
        if extra is not None:
            self.set_content(extra)

        primary, secondary, _close = self.set_buttons(
            spec.primary, spec.secondary, spec.close)
        if primary is not None:
            primary.clicked.connect(
                lambda: self._choose(DialogResult.PRIMARY))
        if secondary is not None:
            secondary.clicked.connect(
                lambda: self._choose(DialogResult.SECONDARY))

        if not spec.dismissible:
            # Escape and the title-bar close would otherwise resolve a
            # destructive question by default, which is exactly backwards.
            # `ContentDialog` has no close affordance of its own unless
            # `set_buttons(close=…)` gave it one, so there is nothing to remove
            # beyond the window hint and the Escape key handled below.
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

    def _build_extra(self, spec: DialogSpec) -> QWidget | None:
        if not spec.footnote and spec.remember is None:
            return None
        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["s"])

        if spec.footnote:
            note = QLabel(spec.footnote, holder)
            note.setWordWrap(True)
            column.addWidget(note)
        if spec.remember is not None:
            self._remember_box = QCheckBox(DIALOG.FIRST_DELETE_OPT, holder)
            column.addWidget(self._remember_box)
        return holder

    def _choose(self, result: DialogResult) -> None:
        self.result_choice = result
        if self._remember_box is not None and self._remember_box.isChecked() \
                and self.spec.remember is not None:
            remember_choice(self.spec.remember)

    def keyPressEvent(self, event: Any) -> None:
        """Escape closes a dialog only when it is allowed to.

        A question about deleting four thousand files must be answered, not
        dismissed — and Escape resolving it to the destructive default is the
        way that goes wrong.
        """
        if (event.key() == Qt.Key.Key_Escape and not self.spec.dismissible):
            event.ignore()
            return
        super().keyPressEvent(event)

    # ── convenience ─────────────────────────────────────────────────────────
    def chose_primary(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY

    def remember_box(self) -> QCheckBox | None:
        return self._remember_box
