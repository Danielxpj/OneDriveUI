"""The Activity Center — OneDrive's flyout, as a real top-level window.

Why this is a window and not a popup
------------------------------------
Everything a tray-anchored popup needs is missing on this desktop, and all three
facts were measured rather than assumed (`ARCHITECTURE.md` §4.1):

  * `QSystemTrayIcon.geometry()` returns a **null rect** under the GNOME
    AppIndicator extension, so there is nothing to anchor to;
  * `QCursor.pos()` reports **(0, 0)** on Wayland outside a drag, so the pointer
    cannot stand in for the anchor either;
  * a `Qt.Popup` opened without a live input serial is **dismissed by Mutter in
    under 300 ms**, so the flyout would vanish before it was read.

So this is a normal top-level `Qt.Tool` window: frameless and translucent, fixed
at `constants.ACTIVITY_CENTER_WIDTH`, placed by us at the bottom-right of the
work area. It carries **no** `QGraphicsDropShadowEffect` and deliberately does
**not** set `Qt.NoDropShadowWindowHint`: the effect paints inside the widget it
is attached to, so a Fluent `shadow16` would cost 32 px of reserved margin on
every side and the frame is specified as exactly 360 px wide. A real top-level
window has a compositor to draw its elevation — that hint exists only to stop a
*second* shadow when a widget draws its own, which is not the case here.

The rounded body is an ordinary **child** `QFrame` carrying the frozen `#Flyout`
rule rather than a `border-radius` on the window itself: QSS rounding on a
top-level translucent window leaves square corners on some Wayland compositors,
while a child widget clips correctly (the same recipe `ContentDialog` uses).

Where every word comes from
---------------------------
Nothing in this module words anything. The headline, the second line, the window
tooltip and the banner all resolve through one status source — `sync/reducer.py`'s
`status_text()` / `tooltip()` when the engine injects it, and :class:`StatusTables`
(the frozen `strings.STATUS_LINE` / `STATUS_SUB` tables, nothing else) before it
exists. That is what makes the four surfaces structurally unable to disagree, and
`tests/test_ui_activity_center.py` greps this file to prove no wording escaped
into it.

Where every action goes
-----------------------
Two routes, no third. A world-changing command goes to `Supervisor.do()` (or to
the two pause entry points the frozen `Supervisor` signature declares); a banner
action button goes onto `BUS.notification_action`, where `ui/notices.py` picks it
up. Navigation that changes nothing — opening Settings or Help — is a widget
signal for the composition root to wire. No service is ever called directly, and
the test module asserts that with an AST walk rather than a hopeful comment.
"""

from __future__ import annotations

import logging

from typing import Any, Protocol, Sequence

from PySide6.QtCore import QPoint, QRect, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from onedriveui import units
from onedriveui.bus import BUS
from onedriveui.constants import (
    ACTIVITY_CENTER_WIDTH, WEB_GET_MORE_STORAGE, WEB_RECYCLE_BIN, WEB_ROOT,
)
from onedriveui.models import (
    AccountInfo, Facts, NotificationId, QuotaInfo, RecoveryAction, SyncState,
    parse_iso,
)
from onedriveui.strings import (
    ACTION_LABEL, DIALOG, FIRST_SYNC_BANNER, MENU, status_line, status_sub, toast,
)
from onedriveui.ui import fonts, icons, motion, qss, theme
from onedriveui.ui.activity_model import ActivityModel
from onedriveui.ui.theme import METRICS, OBJ, PROP, SPACING
from onedriveui.ui.widgets.chrome import StatusGlyph
from onedriveui.ui.widgets.containers import InfoBar, InfoBarSeverity
from onedriveui.ui.widgets.controls import ButtonVariant, FluentButton, icon_button
from onedriveui.ui.widgets.indicators import (
    Avatar, FluentProgressBar, ProgressTone, StorageBar,
)
from onedriveui.ui.widgets.lists import ActivityListView


log = logging.getLogger(__name__)

__all__ = [
    "StatusSource", "StatusTables", "status_format_args", "pause_remaining",
    "BANNER_TOAST", "BANNER_SEVERITY", "STATUS_ACTION", "RESUME_STATES",
    "FIRST_SYNC_STATES", "FOOTER_COMMANDS", "ACTION_URL", "PROGRESS_TONE",
    "ActivityCenter",
]


# ═════════════════════════════════════════════════════════════════════════════
# Status resolution. The reducer is pure and clock-free; so is this fallback.
# ═════════════════════════════════════════════════════════════════════════════

class StatusSource(Protocol):
    """The slice of `sync/reducer.py` this window renders from.

    `CONTRACTS.md` §10.5 freezes both signatures. Injecting the module itself is
    the production wiring; :class:`StatusTables` stands in until WP-05 lands.
    """

    def status_text(self, state: SyncState, facts: Facts) -> tuple[str, str]:
        """-> (headline, second line) for a state."""

    def tooltip(self, state: SyncState, facts: Facts) -> str:
        """-> the hover tooltip for a state."""


def pause_remaining(facts: Facts) -> tuple[int, int] | None:
    """How long a timed manual pause has left, as ``(hours, minutes)``.

    Measured between two fields of the same `Facts` — the pause deadline and the
    instant the facts were sampled — so this stays as clock-free as the reducer
    it substitutes for.

    Args:
        facts: The observation to read `pause.until` and `sampled_at` from.

    Returns:
        ``(hours, minutes)``, or None for an indefinite pause, an unparseable
        stamp, or a deadline that has already passed.
    """
    until = parse_iso(facts.pause.until)
    sampled = parse_iso(facts.sampled_at)
    if until is None or sampled is None:
        return None
    seconds = int((until - sampled).total_seconds())
    if seconds <= 0:
        return None
    return seconds // 3600, (seconds % 3600) // 60


def status_format_args(state: SyncState, facts: Facts) -> dict[str, Any]:
    """The placeholders one state's frozen templates expect.

    The same name means different things in different rows of the table —
    ``{total}`` is a **file count** while transferring and a **byte total** when
    idle — so the arguments are assembled per state rather than once.

    Args:
        state: The state whose templates are about to be formatted.
        facts: The observation the numbers come from.

    Returns:
        A mapping safe to splat into `strings.status_line` / `status_sub`.
    """
    quota = facts.quota
    used = units.human_bytes(quota.used)
    stored = units.human_bytes(quota.total)
    if state is SyncState.SYNCING:
        done = facts.transfers_active
        return {"n": facts.transferring_count, "done": done,
                "total": done + facts.uploads_queued,
                "bytes": used, "size": stored}
    if state in (SyncState.UP_TO_DATE, SyncState.INFO_NOTICE):
        return {"used": used, "total": stored}
    if state is SyncState.WARNING:
        return {"n": facts.issues_error}
    if state in (SyncState.ERROR, SyncState.NEEDS_ATTENTION):
        return {"n": facts.issues_blocking}
    if state is SyncState.PAUSED_MANUAL:
        remaining = pause_remaining(facts)
        hours, minutes = remaining or (0, 0)
        return {"n": facts.transferring_count, "hh": hours, "mm": minutes}
    return {"n": facts.transferring_count, "pct": int(round(quota.pct))}


class StatusTables:
    """`reducer.status_text()` / `tooltip()` rebuilt from the WP-00 tables alone.

    WP-12 is a wave ahead of WP-05, so the flyout has to render before
    `sync/reducer.py` exists. This reads the same frozen tables the reducer will
    read and formats them with the same numbers, which keeps the fallback and the
    real thing in step by construction: neither can invent a wording, because
    neither holds one.
    """

    def status_text(self, state: SyncState, facts: Facts) -> tuple[str, str]:
        """-> (headline, second line). The second line is often empty."""
        args = status_format_args(state, facts)
        headline = status_line(state, **args)
        if state is SyncState.PAUSED_MANUAL and pause_remaining(facts) is None:
            # "Until I resume" has no deadline, so its countdown template has
            # nothing to say and must not render its own placeholders.
            return headline, ""
        return headline, status_sub(state, **args)

    def tooltip(self, state: SyncState, facts: Facts) -> str:
        """-> the two-line hover text, exactly what the tray shows."""
        headline, subtext = self.status_text(state, facts)
        return f"{headline}\n{subtext}".strip()


# ═════════════════════════════════════════════════════════════════════════════
# State-driven tables. Every value is a frozen WP-00 symbol.
# ═════════════════════════════════════════════════════════════════════════════

#: state -> the catalogued toast whose summary, body and action buttons the
#: banner renders.
#:
#: The set is deliberately small, and it is sourced rather than guessed. A banner
#: earns its space only where a state carries something the status strip cannot:
#: `ARCHITECTURE.md` §6.6 gives `PAUSED_QUOTA` a "persistent InfoBar", and
#: `docs/research/onedrive-features.md` §2.3 gives the metered and battery pauses
#: a banner carrying the "Sync Anyway" override, while §2.5's stack shows an
#: "error banner (only when errors exist)".
#:
#: Everything else that *could* have one deliberately does not. `AUTH_REQUIRED`,
#: `ACCOUNT_BLOCKED`, `PAUSED_MANUAL` and `OFFLINE` all already carry a single
#: contextual command in the status strip (see :data:`STATUS_ACTION` and
#: :data:`RESUME_STATES`), which is what §2.3 shows for them — a banner as well
#: would repeat the headline and offer the same button twice. `ERROR` and
#: `NEEDS_ATTENTION` depend on *which* hazard fired, so `ui/notices.py` supplies
#: those through :meth:`ActivityCenter.set_banner` rather than a guess baked in
#: here.
BANNER_TOAST: dict[SyncState, NotificationId] = {
    SyncState.PAUSED_METERED:  NotificationId.SYNC_PAUSED_METERED,
    SyncState.PAUSED_BATTERY:  NotificationId.SYNC_PAUSED_BATTERY,
    SyncState.PAUSED_QUOTA:    NotificationId.QUOTA_FULL,
    SyncState.WARNING:         NotificationId.SYNC_ISSUES,
}

#: state -> the banner severity. A state with no entry shows no automatic banner.
BANNER_SEVERITY: dict[SyncState, InfoBarSeverity] = {
    SyncState.INITIALIZING:    InfoBarSeverity.INFORMATIONAL,
    SyncState.PROCESSING:      InfoBarSeverity.INFORMATIONAL,
    SyncState.INFO_NOTICE:     InfoBarSeverity.INFORMATIONAL,
    SyncState.PAUSED_METERED:  InfoBarSeverity.WARNING,
    SyncState.PAUSED_BATTERY:  InfoBarSeverity.WARNING,
    SyncState.WARNING:         InfoBarSeverity.WARNING,
    SyncState.PAUSED_QUOTA:    InfoBarSeverity.ERROR,
}

#: The two states that carry the first-run reconciliation banner.
FIRST_SYNC_STATES: frozenset[SyncState] = frozenset({
    SyncState.INITIALIZING, SyncState.PROCESSING,
})

#: state -> the single contextual command beside the status line. Its label is
#: `strings.ACTION_LABEL`, and pressing it reaches `Supervisor.do()`.
#:
#: A state that has a banner is **absent** here: the banner already carries the
#: action, and `PAUSED_QUOTA` offering "get more storage" from the status strip,
#: from its own persistent InfoBar and from the storage block's link put the same
#: command on screen three times.
STATUS_ACTION: dict[SyncState, RecoveryAction] = {
    SyncState.SIGNED_OUT:      RecoveryAction.SIGN_IN,
    SyncState.AUTH_REQUIRED:   RecoveryAction.SIGN_IN,
    SyncState.ACCOUNT_BLOCKED: RecoveryAction.OPEN_WEB,
    SyncState.ERROR:           RecoveryAction.RETRY,
    SyncState.OFFLINE:         RecoveryAction.RETRY,
}

#: The state whose contextual command is "resume", which is not a
#: `RecoveryAction` — the frozen `Supervisor` declares `request_resume()` for it.
#:
#: Only the **manual** pause. A metered or battery pause is lifted by the "Sync
#: Anyway" override in its own banner, which is a different thing: it suspends
#: one policy for this session rather than clearing a pause the user asked for.
RESUME_STATES: frozenset[SyncState] = frozenset({SyncState.PAUSED_MANUAL})

#: An action -> the web target it opens, for the ones that take one.
ACTION_URL: dict[RecoveryAction, str] = {
    RecoveryAction.OPEN_WEB:         WEB_ROOT,
    RecoveryAction.GET_MORE_STORAGE: WEB_GET_MORE_STORAGE,
}

#: state -> the tone the inline progress bar paints in.
PROGRESS_TONE: dict[SyncState, ProgressTone] = {
    SyncState.PAUSED_MANUAL:  ProgressTone.PAUSED,
    SyncState.PAUSED_METERED: ProgressTone.PAUSED,
    SyncState.PAUSED_BATTERY: ProgressTone.PAUSED,
    SyncState.PAUSED_QUOTA:   ProgressTone.ERROR,
    SyncState.ERROR:          ProgressTone.ERROR,
    SyncState.WARNING:        ProgressTone.ERROR,
}

#: (glyph key, tooltip, action, url) for the three world-touching footer
#: commands. "Recycle bin" is a **web deep-link** and nothing else: on OneDrive
#: `operations/cleanup` deletes file *versions* rather than the bin, which is
#: invariant I8 and why no rc call appears anywhere near this row.
FOOTER_COMMANDS: tuple[tuple[str, str, RecoveryAction, str], ...] = (
    ("folder",  MENU.OPEN_FOLDER,  RecoveryAction.SHOW_IN_FOLDER, ""),
    ("globe",   MENU.VIEW_ONLINE,  RecoveryAction.OPEN_WEB,       WEB_ROOT),
    ("recycle", MENU.RECYCLE_BIN,  RecoveryAction.OPEN_WEB,       WEB_RECYCLE_BIN),
)

#: The `#Flyout` rule's `SurfaceStrokeColorFlyout` border, in px. Fluent's
#: content padding sits INSIDE the stroke, so the 16 px inset is measured from
#: the surface's content box, not from the window edge.
_FLYOUT_STROKE = 1

_GLYPH_SETTINGS = "settings"
_GLYPH_HELP = "help"
_GLYPH_CLOSE = "close"
_ROLE_STRONG = "body_strong"
_ROLE_BODY = "body"
_ROLE_CAPTION = "caption"

for _key in (_GLYPH_SETTINGS, _GLYPH_HELP, _GLYPH_CLOSE,
             *(row[0] for row in FOOTER_COMMANDS)):
    icons.glyph_stem(_key)                  # raises KeyError on an unknown key
del _key
for _role in (_ROLE_STRONG, _ROLE_BODY, _ROLE_CAPTION):
    theme.font_px(_role)                    # raises KeyError on an unknown role
del _role

_missing = [state.name for state in BANNER_TOAST if state not in BANNER_SEVERITY]
if _missing:                                            # pragma: no cover
    raise ValueError(f"activity_center: BANNER_SEVERITY is missing {_missing}")
_missing = [state.name for state in STATUS_ACTION
            if STATUS_ACTION[state] not in ACTION_LABEL]
if _missing:                                            # pragma: no cover
    raise ValueError(f"activity_center: STATUS_ACTION lacks a label for {_missing}")
# One state, one command. A state that both banners an action AND puts a button
# in the status strip shows the user the same thing twice, which is the defect
# this whole module is arranged to make impossible.
_clash = sorted(state.name for state in
                (set(STATUS_ACTION) | RESUME_STATES) & set(BANNER_TOAST))
if _clash:                                              # pragma: no cover
    raise ValueError(
        f"activity_center: {_clash} would offer an action in the status strip "
        "and in a banner at the same time"
    )
del _missing, _clash


def _wrapping_policy() -> QSizePolicy:
    """A `Preferred/Minimum` policy that actually asks for a wrapped height."""
    policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    policy.setHeightForWidth(True)
    return policy


def _label(text: str, parent: QWidget, *, role: str,
           secondary: bool = False) -> QLabel:
    """A ramp-styled label with an exact line box.

    The role rides a dynamic property so the sheet supplies size and weight; a
    per-widget `setFont()` is recorded by `QStyleSheetStyle` as a custom font and
    survives every repolish, which is how a label ends up ignoring a theme change.
    """
    out = QLabel(text, parent)
    qss.set_property(out, PROP.TYPE, role)
    if secondary:
        qss.set_property(out, PROP.ROLE, "secondary")
    out.setFixedHeight(fonts.line_height(role))
    out.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# ActivityCenter
# ═════════════════════════════════════════════════════════════════════════════

class ActivityCenter(QWidget):
    """The 360 px flyout: header, status, storage, banner, activity, footer.

    The header is rendered from `AccountInfo` and is **never** touched by
    :meth:`set_state`. That is not a convention — it is the mechanism behind
    Microsoft's MC333940 change ("the account name will always be reflected in
    the title even during error scenarios"): with two accounts signed in, a
    flyout that drops the name in `ERROR` leaves the user unable to tell which
    OneDrive is broken. `test_the_header_shows_the_account_name_in_every_state`
    drives all eighteen states and reads the label back.

    Args:
        account: Whose flyout this is. Its `display_name` and `email` are the
            header, and its `sync_root` is what "open folder" opens.
        supervisor: The frozen `sync.supervisor.Supervisor` surface — `do()`,
            `snapshot()`, `state()`, `request_pause()`, `request_resume()`.
            Injected, never imported: WP-05 is a wave behind this one.
        quota: The frozen `sync.quota.QuotaService` surface, or None to read the
            quota out of `Facts` instead.
        status: A :class:`StatusSource`. Defaults to :class:`StatusTables`.
        model: An :class:`~onedriveui.ui.activity_model.ActivityModel` to show.
            One is built and attached to the bus when none is given.
        parent: Left None in production — this is a top-level window.
    """

    #: The gear, and the footer's Settings command. Navigation, not a change.
    settings_requested = Signal()
    #: The footer's Help command.
    help_requested = Signal()
    #: A row was double-clicked or Return-ed. Carries its `QModelIndex`.
    row_activated = Signal(object)
    #: The window was dismissed (Esc, or a close).
    dismissed = Signal()

    #: Verbatim from `constants`: the flyout never resizes horizontally.
    WIDTH: int = ACTIVITY_CENTER_WIDTH
    #: Content-driven between these, with the activity list taking the slack.
    MIN_HEIGHT: int = 320
    MAX_HEIGHT: int = 620
    #: Distance from the work area's right and bottom edges when placed.
    SCREEN_INSET: int = SPACING["m"]

    HEADER_H: int = METRICS["ac_header_h"]
    STORAGE_H: int = METRICS["ac_storage_h"]
    FOOTER_H: int = METRICS["ac_footer_h"]
    INSET: int = METRICS["ac_inset"]
    #: The optional status strip's floor; it grows for a second line and a bar.
    STATUS_H: int = SPACING["xxl"] + SPACING["l"]
    #: Whitespace between stacked blocks. Fluent separates groups with space.
    SECTION_GAP: int = SPACING["m"]
    #: The status glyph and the footer command glyphs.
    GLYPH: int = SPACING["l"]
    #: The inline progress bar's BOX. `FluentProgressBar` centres its 3 px fill
    #: in whatever height it is given, and a bare 3 px box sitting 2 px under the
    #: headline reads as an underline rather than as a bar.
    PROGRESS_H: int = METRICS["progress_fill_h"] + 2 * SPACING["xs"]
    #: The header avatar and the activity row's file icon.
    AVATAR: int = SPACING["xxxl"]

    def __init__(self,
                 account: AccountInfo,
                 *,
                 supervisor: Any,
                 quota: Any = None,
                 activity: Any = None,
                 status: StatusSource | None = None,
                 model: ActivityModel | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent,
                         Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self._account = account
        self._supervisor = supervisor
        self._quota = quota
        #: The persisted activity rows come from here. Without it the flyout
        #: showed only whatever arrived on the bus while it happened to be open:
        #: `ActivityModel.set_history()` existed, worked, and had no caller, so
        #: the `activity` table was written on every transfer and never read.
        self._activity = activity
        self._status_source: StatusSource = status or StatusTables()
        self._state = SyncState.NOT_RUNNING
        self._facts = Facts(account_id=account.id)
        self._tooltip = ""
        self._banner_override = False
        self._banner_key = ""
        self._banner_actions: tuple[tuple[str, str], ...] = ()
        self._first_run: bool | None = None
        self._owns_model = model is None
        self._model = model if model is not None else ActivityModel(
            self, account_id=account.id)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(self.WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # The rounded body is a CHILD frame, not the window: a QSS border-radius
        # on a translucent top-level window leaves square corners on some
        # Wayland compositors, and every `#Flyout QLabel` / `#Flyout QPushButton`
        # rule in the frozen sheet needs this object name as an ancestor anyway.
        self._surface = QFrame(self)
        self._surface.setObjectName(OBJ.FLYOUT)
        self._surface.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._surface)

        body = QVBoxLayout(self._surface)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_header())
        body.addWidget(self._build_status())
        body.addWidget(self._build_storage())
        body.addWidget(self._build_banner_holder())
        body.addWidget(self._build_list(), 1)
        body.addWidget(self._build_footer())

        self.set_account(account)
        if self._owns_model:
            self._model.attach_bus()
        self._connect_bus()
        self.refresh()

    # ── construction ─────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        """Avatar, account name, email, then the gear and the close button at a
        16 px right inset."""
        header = QFrame(self._surface)
        header.setObjectName(OBJ.ACTIVITY_HEADER)
        header.setFixedHeight(self.HEADER_H)
        row = QHBoxLayout(header)
        row.setContentsMargins(self.INSET, 0, self.INSET, 0)
        row.setSpacing(self.SECTION_GAP)

        self._avatar = Avatar(header, diameter=self.AVATAR)
        row.addWidget(self._avatar, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QWidget(header)
        column = QVBoxLayout(text)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["xxs"])
        self._name = _label("", text, role=_ROLE_STRONG)
        self._email = _label("", text, role=_ROLE_CAPTION, secondary=True)
        column.addWidget(self._name)
        column.addWidget(self._email)
        row.addWidget(text, 1, Qt.AlignmentFlag.AlignVCenter)

        self._gear = icon_button(_GLYPH_SETTINGS, header, size=self.GLYPH,
                                 tooltip=MENU.SETTINGS)
        self._gear.setAccessibleName(MENU.SETTINGS)
        self._gear.clicked.connect(self.settings_requested.emit)
        row.addWidget(self._gear, 0, Qt.AlignmentFlag.AlignVCenter)

        # A visible way out. The flyout is a frameless `Qt.Tool` window: it has
        # no titlebar and therefore no close control of its own, and while Esc
        # dismisses it, a keyboard shortcut is not an affordance — nothing on
        # screen says it exists. On this desktop the icon's single click opens
        # the *menu* rather than toggling the flyout, so the icon is not a way
        # back out either. Without this the window can be left with no obvious
        # way to dismiss it.
        self._close = icon_button(_GLYPH_CLOSE, header, size=self.GLYPH,
                                  tooltip=DIALOG.CLOSE)
        self._close.setAccessibleName(DIALOG.CLOSE)
        self._close.clicked.connect(self.close)
        row.addWidget(self._close, 0, Qt.AlignmentFlag.AlignVCenter)
        return header

    def _build_status(self) -> QWidget:
        """The status strip: glyph, headline, second line, bar, one command."""
        strip = QFrame(self._surface)
        strip.setObjectName(OBJ.STATUS_STRIP)
        strip.setMinimumHeight(self.STATUS_H)
        row = QHBoxLayout(strip)
        row.setContentsMargins(self.INSET, self.SECTION_GAP,
                               self.INSET, SPACING["s"])
        row.setSpacing(self.SECTION_GAP)

        # The 16 px glyph is centred on the FIRST LINE, not on the block: the
        # block grows a second line and a bar, and a glyph that drifts with it
        # stops reading as the status line's own icon.
        self._glyph = StatusGlyph(strip, state=self._state, size=self.GLYPH)
        glyph_column = QVBoxLayout()
        glyph_column.setContentsMargins(0, 0, 0, 0)
        glyph_column.setSpacing(0)
        glyph_column.addSpacing(
            max(0, (fonts.line_height(_ROLE_BODY) - self.GLYPH) // 2))
        glyph_column.addWidget(self._glyph)
        glyph_column.addStretch(1)
        row.addLayout(glyph_column, 0)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["xxs"])
        self._headline = _label("", strip, role=_ROLE_BODY)
        self._subtext = QLabel("", strip)
        qss.set_property(self._subtext, PROP.TYPE, _ROLE_CAPTION)
        qss.set_property(self._subtext, PROP.ROLE, "secondary")
        self._subtext.setWordWrap(True)
        self._progress = FluentProgressBar(strip)
        self._progress.setFixedHeight(self.PROGRESS_H)
        self._progress.setVisible(False)
        column.addWidget(self._headline)
        column.addWidget(self._subtext)
        column.addWidget(self._progress)
        row.addLayout(column, 1)

        self._command = FluentButton("", strip, variant=ButtonVariant.STANDARD)
        self._command.setVisible(False)
        self._command.clicked.connect(self._on_command)
        row.addWidget(self._command, 0, Qt.AlignmentFlag.AlignTop)
        return strip

    def _build_storage(self) -> QWidget:
        """"used of total" over the 328 x 4 bar, with the upsell link."""
        block = QFrame(self._surface)
        block.setFixedHeight(self.STORAGE_H)
        column = QVBoxLayout(block)
        column.setContentsMargins(self.INSET, SPACING["s"],
                                  self.INSET, self.SECTION_GAP)
        column.setSpacing(SPACING["s"])

        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(SPACING["s"])
        self._storage_text = _label("", block, role=_ROLE_CAPTION, secondary=True)
        line.addWidget(self._storage_text, 1)
        self._storage_link = FluentButton(
            ACTION_LABEL[RecoveryAction.GET_MORE_STORAGE], block,
            variant=ButtonVariant.HYPERLINK)
        self._storage_link.setFixedHeight(fonts.line_height(_ROLE_CAPTION))
        self._storage_link.clicked.connect(self._on_get_more_storage)
        line.addWidget(self._storage_link, 0)
        column.addLayout(line)

        self._storage_bar = StorageBar(block)
        column.addWidget(self._storage_bar)
        return block

    def _build_banner_holder(self) -> QWidget:
        """The `InfoBar`, in a holder that collapses to nothing when hidden.

        Both widgets get a **height-for-width** size policy. `InfoBar` reports
        `hasHeightForWidth()` (its message label wraps) but ships the default
        `Preferred/Minimum` policy, whose `hasHeightForWidth()` is False — and a
        `QLayout` asks a child for a wrapped height only when the *policy* says
        to. Without this the banner is laid out at its unwrapped height and the
        first-run copy, which is three lines long, is clipped mid-sentence.
        """
        holder = QWidget(self._surface)
        holder.setSizePolicy(_wrapping_policy())
        column = QVBoxLayout(holder)
        column.setContentsMargins(self.INSET, 0, self.INSET, self.SECTION_GAP)
        column.setSpacing(SPACING["s"])
        self._banner = InfoBar("", "", holder, closable=True,
                               close_tooltip=DIALOG.CLOSE)
        self._banner.setSizePolicy(_wrapping_policy())
        self._banner.set_closable(False)
        self._banner.closed.connect(self.clear_banner)
        column.addWidget(self._banner)

        # The action buttons go on their OWN row, which is WinUI's own narrow
        # `InfoBar` reflow. `InfoBar.add_action()` puts them in the horizontal
        # row beside the message, and at this width that is not a layout: a
        # 328 px bar minus the glyph, the close button and a 139 px
        # "get more storage" leaves the message **77 px**, which wraps a short
        # sentence into five stacked words. Measured, not guessed.
        self._banner_action_buttons: list[FluentButton] = []
        self._banner_actions_holder = QWidget(holder)
        row = QHBoxLayout(self._banner_actions_holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING["s"])
        row.addStretch(1)
        self._banner_actions_row = row
        self._banner_actions_holder.setVisible(False)
        column.addWidget(self._banner_actions_holder)

        self._banner_holder = holder
        holder.setVisible(False)
        return holder

    def _build_list(self) -> QWidget:
        """The activity feed. Live rows first; see `activity_model`.

        No section heading and no row separators: `strings.py` has no wording for
        a heading and this package may not invent one, and Fluent lists group by
        whitespace rather than by rules — which is what the viewport margin is.
        """
        self._list = ActivityListView(self._surface, surface="layer")
        self._list.setModel(self._model)
        self._list.setViewportMargins(0, SPACING["s"], 0, SPACING["s"])
        self._list.row_activated.connect(self.row_activated.emit)
        # And act on it. The signal was re-emitted and nothing listened, so
        # double-clicking a row in the activity list did nothing at all —
        # the one gesture every file list in every desktop responds to.
        self._list.row_activated.connect(self._on_row_activated)
        return self._list

    def _on_row_activated(self, index: object) -> None:
        """Reveal the file behind a double-clicked row in the file manager.

        Through `do()`, like every other world-touching action here. A live
        transfer and a persisted event name their path differently, so both are
        asked; a row that names nothing (a daemon-restart marker, say) is simply
        ignored rather than opening the wrong thing.
        """
        row = getattr(index, "row", None)
        if not callable(row):
            return
        position = row()
        source = self._model.source_at(position)
        rel_path = getattr(source, "rel_path", "") or getattr(source, "name", "")
        if not rel_path or self._supervisor is None:
            return
        from pathlib import Path as _Path

        target = _Path(self._account.sync_root).expanduser() / rel_path
        try:
            self._supervisor.do(RecoveryAction.SHOW_IN_FOLDER, path=str(target))
        except Exception:  # noqa: BLE001 - a refusal must not close the flyout
            log.warning("could not reveal %s", target, exc_info=True)

    def _build_footer(self) -> QWidget:
        """Open folder / View online / Recycle bin … Settings / Help."""
        footer = QFrame(self._surface)
        footer.setObjectName(OBJ.FOOTER)
        footer.setFixedHeight(self.FOOTER_H)
        row = QHBoxLayout(footer)
        row.setContentsMargins(self.INSET, 0, self.INSET, 0)
        row.setSpacing(SPACING["s"])

        self._footer_buttons: list[FluentButton] = []
        for glyph, tip, action, url in FOOTER_COMMANDS:
            button = icon_button(glyph, footer, size=self.GLYPH, tooltip=tip)
            button.setAccessibleName(tip)
            button.clicked.connect(
                lambda _checked=False, a=action, u=url: self._on_footer(a, u))
            row.addWidget(button)
            self._footer_buttons.append(button)
        row.addStretch(1)
        for glyph, tip, signal in ((_GLYPH_SETTINGS, MENU.SETTINGS,
                                    self.settings_requested),
                                   (_GLYPH_HELP, MENU.HELP, self.help_requested)):
            button = icon_button(glyph, footer, size=self.GLYPH, tooltip=tip)
            button.setAccessibleName(tip)
            button.clicked.connect(signal.emit)
            row.addWidget(button)
            self._footer_buttons.append(button)
        return footer

    # ── bus ──────────────────────────────────────────────────────────────
    def _connect_bus(self) -> None:
        BUS.state_changed.connect(self._on_state_changed)
        BUS.facts_updated.connect(self._on_facts)
        BUS.quota_updated.connect(self._on_quota)
        BUS.account_updated.connect(self._on_account_updated)
        BUS.theme_changed.connect(self._on_theme_changed)

    def _disconnect_bus(self) -> None:
        for signal, slot in ((BUS.state_changed, self._on_state_changed),
                             (BUS.facts_updated, self._on_facts),
                             (BUS.quota_updated, self._on_quota),
                             (BUS.account_updated, self._on_account_updated),
                             (BUS.theme_changed, self._on_theme_changed)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):            # pragma: no cover
                pass

    @Slot(object, object, object)
    def _on_state_changed(self, _old: SyncState, new: SyncState,
                          facts: Facts) -> None:
        if facts.account_id and facts.account_id != self._account.id:
            return
        self.set_state(new, facts)

    @Slot(object)
    def _on_facts(self, facts: Facts) -> None:
        """A 2.5 Hz tick: the state has not moved but its numbers have.

        The banner is re-rendered too, because some of the catalogued copy
        carries a live count — a stale "N files" under a moving feed is exactly
        the kind of disagreement the single-source rule exists to prevent.
        """
        if facts.account_id and facts.account_id != self._account.id:
            return
        self._facts = facts
        if not self._banner_override:
            self._render_banner()
        self._render_status()
        if self._quota is None:
            self._render_storage(facts.quota)

    @Slot(object)
    def _on_quota(self, quota: QuotaInfo) -> None:
        self._render_storage(quota)

    @Slot(object)
    def _on_account_updated(self, account: AccountInfo) -> None:
        if account.id == self._account.id:
            self.set_account(account)

    @Slot(bool, str)
    def _on_theme_changed(self, _dark: bool, _accent: str) -> None:
        self.refresh_theme()

    # ── public API ───────────────────────────────────────────────────────
    def open_(self, *, anchor: QPoint | None = None) -> None:
        """Show the flyout, placed and fully refreshed.

        Args:
            anchor: The top-left corner to place it at. None uses the bottom
                right of the screen's work area, inset by :attr:`SCREEN_INSET`.

        The entrance is a fade rather than Fluent's fade-plus-16 px rise:
        `motion.rise_in` refuses a top-level window because Wayland's compositor
        owns the position and `QWidget.pos()` reports the request rather than
        reality, and the surface's own position belongs to this window's layout.
        With animations off — both desktop settings are false on this machine —
        the fade collapses to 0 ms and the window simply appears.
        """
        was_visible = self.isVisible()
        self.refresh()
        self.adjust_height()
        self.move(self.placement(anchor))
        self.show()
        self.raise_()
        self.activateWindow()
        if not was_visible:
            # Asking for an open flyout again is a request to bring it forward,
            # not to play its entrance a second time. Fading a window the user
            # is already looking at reads as a blink.
            motion.fade_in(self._surface, duration="flyout")

    def refresh(self) -> None:
        """Re-read the supervisor, the quota and the history, then re-render."""
        self._load_history()
        snapshot = self._supervisor.snapshot()
        if snapshot is not None:
            self.set_state(snapshot.state, snapshot.facts)
        if self._quota is not None:
            self._render_storage(self._quota.current())
        else:
            self._render_storage(self._facts.quota)
        self._model.refresh_times()

    def set_account(self, account: AccountInfo) -> None:
        """Render the header. The **only** thing that writes the account name.

        `set_state()` never touches these three widgets, which is what makes the
        identity survive every error state (MC333940).
        """
        self._account = account
        name = account.display_name or account.email or account.remote
        self._name.setText(name)
        self._email.setText(account.email)
        self._email.setVisible(bool(account.email))
        self._avatar.set_person(name)

    def _load_history(self) -> None:
        """Fill the model from the persisted activity table.

        Read on open rather than held live: the flyout is closed most of the
        time, and re-reading a few dozen rows when it appears is cheaper than
        keeping a subscription alive for a window nobody is looking at.
        """
        if self._activity is None:
            return
        try:
            self._model.set_history(self._activity.recent())
        except Exception:  # noqa: BLE001 - an empty list beats no window
            log.warning("could not read the activity history", exc_info=True)

    def set_state(self, state: SyncState, facts: Facts | None = None) -> None:
        """Move the status strip, the glyph, the tooltip and the banner.

        Args:
            state: The reduced state to render.
            facts: The observation behind it. The previous one is kept when None,
                so a caller can drive a state without assembling `Facts`.
        """
        self._state = SyncState(state)
        if facts is not None:
            self._facts = facts
        self._glyph.set_state(self._state)
        # Banner first: the status strip suppresses a second line the banner is
        # already showing, so it has to be able to see what the banner says.
        if not self._banner_override:
            self._render_banner()
        self._render_status()

    def set_banner(self,
                   title: str = "",
                   message: str = "",
                   *,
                   severity: InfoBarSeverity = InfoBarSeverity.INFORMATIONAL,
                   actions: Sequence[tuple[str, str]] = (),
                   key: str = "",
                   closable: bool = True) -> None:
        """Show an explicit banner, overriding the state's automatic one.

        Args:
            title: Body Strong headline, from `strings.py`.
            message: The detail line, from `strings.py`. Empty title **and**
                message hides the banner.
            severity: One of `InfoBarSeverity`; picks the frozen banner rule.
            actions: ``((action_id, label), …)`` — the shape `NotifySpec` uses.
                Pressing one emits `BUS.notification_action(key, action_id)`,
                which is where `ui/notices.py` picks the command up. At most two,
                because that is what `NotifySpec` allows and GNOME renders.
            key: The `NotificationId` value the actions are reported under.
            closable: Show the dismiss button.

        The override lasts until :meth:`clear_banner`; a later :meth:`set_state`
        will not silently replace a banner the notice router put here.
        """
        self._banner_override = bool(title or message)
        self._apply_banner(title, message, severity=severity, actions=actions,
                           key=key, closable=closable)

    def clear_banner(self) -> None:
        """Drop any explicit banner and fall back to the state's own."""
        self._banner_override = False
        self._render_banner()

    def set_first_run(self, first_run: bool | None) -> None:
        """Force the first-reconciliation banner on or off.

        None restores the derived answer: an account that has never completed a
        sync has no `last_ok_at`, and that is its first run.
        """
        self._first_run = None if first_run is None else bool(first_run)
        if not self._banner_override:
            self._render_banner()

    def is_first_run(self) -> bool:
        """True while this account has never recorded a successful sync."""
        if self._first_run is not None:
            return self._first_run
        return not self._account.last_ok_at

    def adjust_height(self) -> None:
        """Resize to the content, clamped between MIN_HEIGHT and MAX_HEIGHT."""
        self.resize(self.WIDTH, self.preferred_height())

    def preferred_height(self) -> int:
        """The content-driven height the flyout wants, already clamped."""
        self.ensurePolished()
        chrome = (self.HEADER_H + self.STORAGE_H + self.FOOTER_H
                  + max(self.STATUS_H, self._status_height()))
        if not self._banner_holder.isHidden():
            chrome += self._banner_height() + self.SECTION_GAP
        rows = self._model.rowCount() * self._list.delegate().row_height()
        return max(self.MIN_HEIGHT, min(self.MAX_HEIGHT, chrome + rows))

    def placement(self, anchor: QPoint | None = None) -> QPoint:
        """Where to put the window: `anchor`, or the work area's bottom right."""
        if anchor is not None:
            return anchor
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:                               # pragma: no cover
            return QPoint(0, 0)
        area: QRect = screen.availableGeometry()
        return QPoint(area.right() - self.WIDTH - self.SCREEN_INSET + 1,
                      area.bottom() - self.height() - self.SCREEN_INSET + 1)

    # ── reads, for the composition root and for tests ────────────────────
    def account(self) -> AccountInfo:
        return self._account

    def state(self) -> SyncState:
        return self._state

    def facts(self) -> Facts:
        return self._facts

    def model(self) -> ActivityModel:
        return self._model

    def list_view(self) -> ActivityListView:
        return self._list

    def surface(self) -> QFrame:
        """The rounded `#Flyout` body. Exactly :attr:`WIDTH` wide."""
        return self._surface

    def header_name(self) -> str:
        """The account name as drawn. Never empty, and never state-dependent."""
        return self._name.text()

    def header_email(self) -> str:
        return self._email.text()

    def status_headline(self) -> str:
        return self._headline.text()

    def status_subtext(self) -> str:
        return self._subtext.text()

    def status_tooltip(self) -> str:
        """The hover text — the same two lines the tray shows."""
        return self._tooltip

    def storage_text(self) -> str:
        return self._storage_text.text()

    def storage_bar(self) -> StorageBar:
        return self._storage_bar

    def banner(self) -> InfoBar:
        return self._banner

    def banner_visible(self) -> bool:
        """Whether a banner is shown.

        `isHidden()` rather than `isVisible()`: a child of a window that has not
        been shown yet reports `isVisible() == False` however it was configured,
        so the visible-state question has to be asked of the explicit hide flag.
        """
        return not self._banner_holder.isHidden()

    def banner_text(self) -> tuple[str, str]:
        """-> (title, message) currently drawn, or two empty strings."""
        if self._banner_holder.isHidden():
            return "", ""
        return self._banner.title(), self._banner.message()

    def banner_key(self) -> str:
        """The `NotificationId` value the banner's actions report under."""
        return self._banner_key

    def banner_actions(self) -> tuple[FluentButton, ...]:
        """The banner's action buttons, on their own row beneath the bar."""
        return tuple(self._banner_action_buttons)

    def command_button(self) -> FluentButton:
        """The contextual button beside the status line."""
        return self._command

    def settings_button(self) -> FluentButton:
        """The header gear."""
        return self._gear

    def footer_buttons(self) -> tuple[FluentButton, ...]:
        """The five footer commands, left to right."""
        return tuple(self._footer_buttons)

    def storage_link(self) -> FluentButton:
        return self._storage_link

    # ── rendering ────────────────────────────────────────────────────────
    def _status_height(self) -> int:
        """What the status strip needs for the lines it is currently showing."""
        height = fonts.line_height(_ROLE_BODY) + self.SECTION_GAP + SPACING["s"]
        if not self._subtext.isHidden():
            height += (max(fonts.line_height(_ROLE_CAPTION),
                           self._subtext.heightForWidth(self._subtext_width()))
                       + SPACING["xxs"])
        if not self._progress.isHidden():
            height += self.PROGRESS_H + SPACING["xxs"]
        return height

    def _subtext_width(self) -> int:
        """The width the second line wraps at, computed rather than measured.

        `preferred_height()` runs **before** `open_()` shows the window, and an
        unshown child reports Qt's default 100 px whatever the layout intends —
        measuring it would size the flyout from a number that is never true.
        """
        width = (self.WIDTH - 2 * _FLYOUT_STROKE - 2 * self.INSET
                 - self.GLYPH - self.SECTION_GAP)
        if not self._command.isHidden():
            width -= self._command.sizeHint().width() + self.SECTION_GAP
        return max(1, width)

    def _banner_height(self) -> int:
        """The banner block's height: the **wrapped** bar plus its action row."""
        width = self._banner.width() or (self.WIDTH - 2 * self.INSET
                                         - 2 * _FLYOUT_STROKE)
        height = max(self._banner.sizeHint().height(),
                     self._banner.heightForWidth(width))
        if not self._banner_actions_holder.isHidden():
            height += SPACING["s"] + METRICS["button_h"]
        return height

    def _render_status(self) -> None:
        """Headline, second line, tooltip, progress and the one command button.

        The second line is dropped when the banner already carries it. A paused
        state's second line and its banner body are both "why", drawn from the
        same table — printing both puts the same sentence on screen twice, eight
        pixels apart. The **tooltip** keeps both lines regardless: it is the
        tray's own two-line text and must stay whole.
        """
        headline, subtext = self._status_source.status_text(self._state, self._facts)
        self._headline.setText(headline)
        if subtext and self._banner_says(subtext):
            subtext = ""
        self._subtext.setText(subtext)
        self._subtext.setVisible(bool(subtext))
        self._tooltip = self._status_source.tooltip(self._state, self._facts)
        self.setToolTip(self._tooltip)
        self._surface.setToolTip(self._tooltip)

        self._render_progress()
        self._render_command()

    def _banner_says(self, text: str) -> bool:
        """True when the visible banner already carries this wording.

        Compared without a trailing full stop: the same sentence is a fragment in
        `STATUS_SUB` and a complete one in `TOAST`, which is a punctuation
        difference and not a different message.
        """
        if self._banner_holder.isHidden():
            return False
        probe = text.rstrip(".").strip()
        if not probe:
            return False
        return probe in self._banner.title() or probe in self._banner.message()

    def _render_progress(self) -> None:
        """A determinate bar while transferring, indeterminate while busy."""
        busy = self._state in (SyncState.SYNCING, SyncState.PROCESSING,
                               SyncState.MOUNTING, SyncState.INITIALIZING)
        self._progress.setVisible(busy)
        if not busy:
            self._progress.set_indeterminate(False)
            return
        self._progress.set_tone(PROGRESS_TONE.get(self._state, ProgressTone.NORMAL))
        done = self._facts.transfers_active
        total = done + self._facts.uploads_queued
        if self._state is SyncState.SYNCING and total > 0:
            self._progress.set_indeterminate(False)
            self._progress.set_value(done / total)
        else:
            self._progress.set_indeterminate(True)

    def _render_command(self) -> None:
        """The single contextual button: resume, or one `RecoveryAction`.

        Guarded, because this runs on every 2.5 Hz tick and `set_variant()`
        repolishes: `setProperty()` without an unpolish/polish pair leaves the
        old rule matched, so the call is not free.
        """
        label = ""
        if self._state in RESUME_STATES:
            label = MENU.RESUME
        else:
            action = STATUS_ACTION.get(self._state)
            if action is not None:
                label = ACTION_LABEL[action]
        if not label:
            self._command.setVisible(False)
            return
        self._command.setText(label)
        if self._command.variant() is not ButtonVariant.ACCENT:
            self._command.set_variant(ButtonVariant.ACCENT)
        self._command.setVisible(True)

    def _render_storage(self, quota: QuotaInfo) -> None:
        """"used of total" plus the bar. Both come from one `QuotaInfo`."""
        self._storage_text.setText(status_sub(
            SyncState.UP_TO_DATE,
            used=units.human_bytes(quota.used),
            total=units.human_bytes(quota.total)))
        self._storage_bar.set_usage(quota.used, quota.total)
        self._storage_link.setVisible(quota.tier != "ok")

    def _render_banner(self) -> None:
        """The automatic banner for the current state, or none."""
        if self.is_first_run() and self._state in FIRST_SYNC_STATES:
            self._apply_banner("", FIRST_SYNC_BANNER,
                               severity=InfoBarSeverity.INFORMATIONAL,
                               closable=False)
            return
        nid = BANNER_TOAST.get(self._state)
        severity = BANNER_SEVERITY.get(self._state)
        if nid is not None:
            summary, body, actions = toast(
                nid, **status_format_args(self._state, self._facts))
            headline, _subtext = self._status_source.status_text(self._state,
                                                                 self._facts)
            if summary.rstrip(".") == headline.rstrip("."):
                # A catalogued toast repeats the status line as its summary,
                # because a toast is read with no status line beside it. Here
                # the headline is two lines up, so the title would be the same
                # sentence twice — and it is the one label in the bar that does
                # not wrap, so it is also the one that squeezes the message.
                summary = ""
            self._apply_banner(summary, body,
                               severity=severity or InfoBarSeverity.INFORMATIONAL,
                               actions=actions, key=nid.value)
            return
        if self._state is SyncState.INFO_NOTICE and self._facts.info_notice:
            self._apply_banner("", self._facts.info_notice,
                               severity=severity or InfoBarSeverity.INFORMATIONAL)
            return
        self._apply_banner("", "")

    def _apply_banner(self,
                      title: str,
                      message: str,
                      *,
                      severity: InfoBarSeverity = InfoBarSeverity.INFORMATIONAL,
                      actions: Sequence[tuple[str, str]] = (),
                      key: str = "",
                      closable: bool = True) -> None:
        """Fill the `InfoBar` and show or hide its holder.

        The severity swap and the action buttons are rebuilt only when they
        actually change. This runs on every tick — a banner whose copy carries a
        live count has to keep up — and both operations are expensive:
        `set_severity()` renames the object and repolishes, and rebuilding the
        actions destroys and recreates real `QPushButton`s under the pointer.
        """
        self._banner_key = key
        actions = tuple(actions)
        if not (title or message):
            self._set_banner_actions(())
            self._banner_holder.setVisible(False)
            self._banner.set_title("")
            self._banner.set_message("")
            return
        if self._banner.severity() is not severity:
            self._banner.set_severity(severity)
        self._banner.set_title(title)
        self._banner.set_message(message)
        self._banner.set_closable(bool(closable))
        self._set_banner_actions(actions)
        self._banner_holder.setVisible(True)

    def _set_banner_actions(self,
                            actions: tuple[tuple[str, str], ...]) -> None:
        """Rebuild the banner's action row, but only when it actually changed."""
        if actions == self._banner_actions:
            return
        for button in self._banner_action_buttons:
            self._banner_actions_row.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._banner_action_buttons.clear()
        for index, (action_id, label) in enumerate(actions):
            button = FluentButton(
                label, self._banner_actions_holder,
                variant=(ButtonVariant.ACCENT if index == 0
                         else ButtonVariant.STANDARD))
            button.clicked.connect(
                lambda _checked=False, a=action_id: self._on_banner_action(a))
            self._banner_actions_row.addWidget(button)
            self._banner_action_buttons.append(button)
        self._banner_actions_holder.setVisible(bool(actions))
        self._banner_actions = actions

    def refresh_theme(self) -> None:
        """Re-polish after a theme change; custom painters re-read tokens."""
        qss.repolish(self, deep=True)
        self._list.refresh_theme()
        self._avatar.update()
        self._storage_bar.update()

    # ── actions. Two routes out of this window, and no third. ────────────
    def _on_command(self) -> None:
        """The status strip's contextual button."""
        if self._state in RESUME_STATES:
            self._supervisor.request_resume()
            return
        action = STATUS_ACTION.get(self._state)
        if action is None:                               # pragma: no cover
            return
        url = ACTION_URL.get(action, "")
        if url:
            self._supervisor.do(action, url=url)
        else:
            self._supervisor.do(action)

    def _on_get_more_storage(self) -> None:
        self._supervisor.do(RecoveryAction.GET_MORE_STORAGE,
                            url=WEB_GET_MORE_STORAGE)

    def _on_footer(self, action: RecoveryAction, url: str) -> None:
        if action is RecoveryAction.SHOW_IN_FOLDER:
            self._supervisor.do(action, path=self._account.sync_root)
            return
        self._supervisor.do(action, url=url)

    def _on_banner_action(self, action_id: str) -> None:
        """A banner button. `ui/notices.py` owns what each id means."""
        BUS.notification_action.emit(self._banner_key, action_id)

    # ── window behaviour ─────────────────────────────────────────────────
    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Esc dismisses, exactly as the Windows flyout does."""
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.dismissed.emit()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Drop every bus connection. `BUS` is a process-wide singleton, so a
        window that is thrown away without this keeps receiving ticks."""
        self._disconnect_bus()
        if self._owns_model:
            self._model.detach_bus()
